#
# Copyright 2018 Analytics Zoo Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import math

from zoo.pipeline.inference import InferenceModel
from zoo.orca.data import SparkXShards
from zoo.orca.learn.spark_estimator import Estimator as SparkEstimator
from zoo import get_node_and_core_number
from zoo.util import nest
from zoo.common.nncontext import init_nncontext

import numpy as np


class Estimator(object):
    @staticmethod
    def from_openvino(*, model_path, batch_size=0):
        """
        Load an openVINO Estimator.

        :param model_path: String. The file path to the OpenVINO IR xml file.
        :param batch_size: Int. Set batch Size, default is 0 (use default batch size).
        """
        return OpenvinoEstimator(model_path=model_path, batch_size=batch_size)


class OpenvinoEstimator(SparkEstimator):
    def __init__(self,
                 *,
                 model_path,
                 batch_size=0):
        self.node_num, self.core_num = get_node_and_core_number()
        self.path = model_path
        if batch_size != 0:
            self.batch_size = batch_size
        else:
            import xml.etree.ElementTree as ET
            tree = ET.parse(model_path)
            root = tree.getroot()
            shape_item = root.find('./layers/layer/output/port/dim[1]')
            if shape_item is None:
                raise ValueError("Invalid openVINO IR xml file, please check your model_path")
            self.batch_size = int(shape_item.text)
        self.model = InferenceModel(supported_concurrent_num=self.core_num)
        self.model.load_openvino_ng(model_path=model_path,
                                    weight_path=model_path[:model_path.rindex(".")] + ".bin",
                                    batch_size=self.batch_size)

    def fit(self, data, epochs, batch_size=32, feature_cols=None, label_cols=None,
            validation_data=None, checkpoint_trigger=None):
        """
        Fit is not supported in OpenVINOEstimator
        """
        raise NotImplementedError

    def predict(self, data, feature_cols=None):
        """
        Predict input data

        :param data: data to be predicted. XShards, Spark DataFrame, numpy array and list of numpy
               arrays are supported. If data is XShards, each partition is a dictionary of  {'x':
               feature}, where feature(label) is a numpy array or a list of numpy arrays.
        :param feature_cols: Feature column name(s) of data. Only used when data is a Spark
               DataFrame. Default: None.
        :return: predicted result.
                 If the input data is XShards, the predict result is a XShards, each partition
                 of the XShards is a dictionary of {'prediction': result}, where the result is a
                 numpy array or a list of numpy arrays.
                 If the input data is numpy arrays or list of numpy arrays, the predict result is
                 a numpy array or a list of numpy arrays.
        """
        from pyspark.sql import DataFrame

        def predict_transform(dict_data, batch_size):
            assert isinstance(dict_data, dict), "each shard should be an dict"
            assert "x" in dict_data, "key x should in each shard"
            feature_data = dict_data["x"]
            if isinstance(feature_data, np.ndarray):
                assert feature_data.shape[0] <= batch_size, \
                    "The batch size of input data (the second dim) should be less than the model " \
                    "batch size, otherwise some inputs will be ignored."
            elif isinstance(feature_data, list):
                for elem in feature_data:
                    assert isinstance(elem, np.ndarray), "Each element in the x list should be " \
                                                         "a ndarray, but get " + \
                                                         elem.__class__.__name__
                    assert elem.shape[0] <= batch_size, "The batch size of each input data (the " \
                                                        "second dim) should be less than the " \
                                                        "model batch size, otherwise some inputs " \
                                                        "will be ignored."
            else:
                raise ValueError("x in each shard should be a ndarray or a list of ndarray.")
            return feature_data

        sc = init_nncontext()

        if isinstance(data, DataFrame):
            from zoo.orca.learn.utils import dataframe_to_xshards, convert_predict_rdd_to_dataframe
            xshards, _ = dataframe_to_xshards(data,
                                              validation_data=None,
                                              feature_cols=feature_cols,
                                              label_cols=None,
                                              mode="predict")
            transformed_data = xshards.transform_shard(predict_transform, self.batch_size)
            result_rdd = self.model.distributed_predict(transformed_data.rdd, sc)

            def delete_useless_result(data):
                shard, y = data
                data_length = len(shard["x"])
                return y[: data_length]
            result_rdd = xshards.rdd.zip(result_rdd).map(delete_useless_result)
            return convert_predict_rdd_to_dataframe(data, result_rdd.flatMap(lambda data: data))
        elif isinstance(data, SparkXShards):
            transformed_data = data.transform_shard(predict_transform, self.batch_size)
            result_rdd = self.model.distributed_predict(transformed_data.rdd, sc)

            def update_shard(data):
                shard, y = data
                data_length = len(shard["x"])
                shard["prediction"] = y[: data_length]
                return shard
            return SparkXShards(data.rdd.zip(result_rdd).map(update_shard))
        elif isinstance(data, (np.ndarray, list)):
            if isinstance(data, np.ndarray):
                split_num = math.ceil(len(data)/self.batch_size)
                arrays = np.array_split(data, split_num)
                data_length_list = list(map(lambda arr: len(arr), arrays))
                data_rdd = sc.parallelize(arrays, numSlices=split_num)
            elif isinstance(data, list):
                flattened = nest.flatten(data)
                data_length = len(flattened[0])
                data_to_be_rdd = []
                split_num = math.ceil(flattened[0].shape[0]/self.batch_size)
                for i in range(split_num):
                    data_to_be_rdd.append([])
                for x in flattened:
                    assert isinstance(x, np.ndarray), "the data in the data list should be " \
                                                      "ndarrays, but get " + \
                                                      x.__class__.__name__
                    assert len(x) == data_length, \
                        "the ndarrays in data must all have the same size in first dimension" \
                        ", got first ndarray of size {} and another {}".format(data_length, len(x))
                    x_parts = np.array_split(x, split_num)
                    for idx, x_part in enumerate(x_parts):
                        data_to_be_rdd[idx].append(x_part)
                        data_length_list = list(map(lambda arr: len(arr), x_part))

                data_to_be_rdd = [nest.pack_sequence_as(data, shard) for shard in data_to_be_rdd]
                data_rdd = sc.parallelize(data_to_be_rdd, numSlices=split_num)

            result_rdd = self.model.distributed_predict(data_rdd, sc)
            result_arr_list = result_rdd.collect()
            for i in range(0, len(result_arr_list)):
                result_arr_list[i] = result_arr_list[i][:data_length_list[i]]
            result_arr = np.concatenate(result_arr_list, axis=0)
            return result_arr
        else:
            raise ValueError("Only XShards, Spark DataFrame, a numpy array and a list of numpy arr"
                             "ays are supported as input data, but get " + data.__class__.__name__)

    def evaluate(self, data, batch_size=32, feature_cols=None, label_cols=None):
        """
        Evaluate is not supported in OpenVINOEstimator
        """
        raise NotImplementedError

    def get_model(self):
        """
        Get_model is not supported in OpenVINOEstimator
        """
        raise NotImplementedError

    def save(self, model_path):
        """
        Save is not supported in OpenVINOEstimator
        """
        raise NotImplementedError

    def load(self, model_path, batch_size=0):
        """
        Load an openVINO model.

        :param model_path: String. The file path to the OpenVINO IR xml file.
        :param batch_size: Int. Set batch Size, default is 0 (use default batch size).
        :return:
        """
        self.node_num, self.core_num = get_node_and_core_number()
        self.path = model_path
        if batch_size != 0:
            self.batch_size = batch_size
        else:
            import xml.etree.ElementTree as ET
            tree = ET.parse(model_path)
            root = tree.getroot()
            shape_item = root.find('./layers/layer/output/port/dim[1]')
            if shape_item is None:
                raise ValueError("Invalid openVINO IR xml file, please check your model_path")
            self.batch_size = int(shape_item.text)
        self.model = InferenceModel(supported_concurrent_num=self.core_num)
        self.model.load_openvino(model_path=model_path,
                                 weight_path=model_path[:model_path.rindex(".")] + ".bin",
                                 batch_size=batch_size)

    def set_tensorboard(self, log_dir, app_name):
        """
        Set_tensorboard is not supported in OpenVINOEstimator
        """
        raise NotImplementedError

    def clear_gradient_clipping(self):
        """
        Clear_gradient_clipping is not supported in OpenVINOEstimator
        """
        raise NotImplementedError

    def set_constant_gradient_clipping(self, min, max):
        """
        Set_constant_gradient_clipping is not supported in OpenVINOEstimator
        """
        raise NotImplementedError

    def set_l2_norm_gradient_clipping(self, clip_norm):
        """
        Set_l2_norm_gradient_clipping is not supported in OpenVINOEstimator
        """
        raise NotImplementedError

    def get_train_summary(self, tag=None):
        """
        Get_train_summary is not supported in OpenVINOEstimator
        """
        raise NotImplementedError

    def get_validation_summary(self, tag=None):
        """
        Get_validation_summary is not supported in OpenVINOEstimator
        """
        raise NotImplementedError

    def load_orca_checkpoint(self, path, version):
        """
        Load_orca_checkpoint is not supported in OpenVINOEstimator
        """
        raise NotImplementedError
