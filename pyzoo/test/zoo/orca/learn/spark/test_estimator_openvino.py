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
import os
import tarfile
import tempfile
from unittest import TestCase

import numpy as np
from bigdl.dataset.base import maybe_download

from zoo import init_nncontext
from zoo.orca.data import SparkXShards
from zoo.orca.learn.openvino import Estimator

property_path = os.path.join(os.path.split(__file__)[0],
                             "../../../../../../zoo/target/classes/app.properties")
data_url = "http://10.239.45.10:8081/repository/raw"
resource_path = os.path.join(os.path.split(__file__)[0], "../../../resources")

with open(property_path) as f:
    for _ in range(2):  # skip the first two lines
        next(f)
    for line in f:
        if "inner-ftp-uri" in line:
            line = line.strip()
            data_url = line.split("=")[1].replace("\\", "")


def read_file_and_cast(file_path):
    with open(file_path, "r") as file:
        d = file.readline()
        data_list = list(map(lambda s: float(s), d.split(", ")))
        return data_list


class TestEstimatorForOpenVINO(TestCase):
    def setUp(self):
        input_file_path = os.path.join(resource_path, "orca/learn/resnet_input")
        output_file_path = os.path.join(resource_path, "orca/learn/resnet_output")
        self.input = read_file_and_cast(input_file_path)
        self.output = read_file_and_cast(output_file_path)
        self.input = np.array(self.input).reshape([3, 224, 224])
        self.output = np.array(self.output).reshape([4, 1000])[:1]

        with tempfile.TemporaryDirectory() as local_path:
            model_url = data_url + "/analytics-zoo-data/openvino2020_resnet50.tar"
            model_path = maybe_download("openvino2020_resnet50.tar",
                                        local_path, model_url)
            tar = tarfile.open(model_path)
            tar.extractall(path=local_path)
            tar.close()
            model_path = os.path.join(local_path, "openvino2020_resnet50/resnet_v1_50.xml")
            self.est = Estimator.from_openvino(model_path=model_path)

    def check_result(self, result, length=0):
        if length == 0:
            length = len(result)
        return np.all(list(map(lambda i: np.allclose(result[i], self.output), range(0, length))))

    def test_openvino_predict_ndarray(self):
        input_data = np.array([self.input] * 22)
        result = self.est.predict(input_data)
        assert isinstance(result, np.ndarray)
        assert result.shape == (22, 1000)
        assert self.check_result(result, 22)

    def test_openvino_predict_xshards(self):
        input_data_list = [np.array([self.input] * 4), np.array([self.input] * 2)]
        sc = init_nncontext()
        rdd = sc.parallelize(input_data_list, numSlices=2)
        shards = SparkXShards(rdd)

        def pre_processing(images):
            return {"x": images}

        shards = shards.transform_shard(pre_processing)
        result = self.est.predict(shards)
        result_c = result.collect()
        assert isinstance(result, SparkXShards)
        assert result_c[0]["prediction"].shape == (4, 1000)
        assert result_c[1]["prediction"].shape == (2, 1000)
        assert self.check_result(result_c[0]["prediction"], 4)
        assert self.check_result(result_c[1]["prediction"], 2)

    def test_openvino_predict_spark_df(self):
        from pyspark.sql import SparkSession

        sc = init_nncontext()
        spark = SparkSession(sc)
        input_list = self.input.tolist()
        rdd = sc.range(0, 18, numSlices=3)
        input_df = rdd.map(lambda x: [input_list]).toDF(["feature"])
        with self.assertRaises(Exception):
            self.est.predict(input_df, feature_cols=["feature"])
        rdd = sc.range(0, 18, numSlices=5)
        input_df = rdd.map(lambda x: [input_list]).toDF(["feature"])
        result_df = self.est.predict(input_df, feature_cols=["feature"])
        result = list(map(lambda row: np.array(row["prediction"]),
                          result_df.select("prediction").collect()))
        assert np.array(result_df.select("prediction").first()).shape == (1, 1000)
        assert result_df.count() == 18
        assert self.check_result(result, 18)


if __name__ == "__main__":
    import pytest

    pytest.main([__file__])
