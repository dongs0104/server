# Copyright (c) 2024, NVIDIA CORPORATION. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#  * Neither the name of NVIDIA CORPORATION nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS ``AS IS'' AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY
# OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import json
from copy import deepcopy
from enum import Enum
from typing import List

import tritonserver
from pydantic import field_validator
from pydantic.dataclasses import dataclass
from tritonfrontend._api._error_mapping import handle_triton_error
from tritonfrontend._c.tritonfrontend_bindings import InvalidArgumentError


class Feature(Enum):
    """
    List of Features that are provided by KServeHttp and KServeGrpc Endpoints for the Server.
    1-to-1 copy of RestrictedCategory Enum from https://github.com/triton-inference-server/server/blob/main/src/restricted_features.h
    """

    HEALTH = "health"
    METADATA = "metadata"
    INFERENCE = "inference"
    SHM_MEMORY = "shared-memory"
    MODEL_CONFIG = "model-config"
    MODEL_REPOSITORY = "model-repository"
    STATISTICS = "statistics"
    TRACE = "trace"
    LOGGING = "logging"


@dataclass
class FeatureGroup:
    """
    Stores instances of (key, value, features) and performs type validation on instance.
    Used by the RestrictedFeatures Class.

    Example:
        >>> from tritonfrontend import Feature, FeatureGroup
        >>> infer_group = FeatureGroup("infer-key", "infer-value", [Feature.INFERENCE])
        >>> health_group = FeatureGroup("key", "value", ["health"]) # Will Error Out
        Invalid features found: ['health'] ... Valid options are: ['Feature.HEALTH',
        'Feature.METADATA', 'Feature.INFERENCE', 'Feature.SHM_MEMORY', 'Feature.MODEL_CONFIG',
        'Feature.MODEL_REPOSITORY', 'Feature.STATISTICS', 'Feature.TRACE', 'Feature.LOGGING']
    """

    key: str
    value: str
    features: List[Feature]

    @field_validator("features", mode="before")
    def validate_features(features: List[Feature]) -> List[Feature]:
        invalid_features = [item for item in features if not isinstance(item, Feature)]
        if invalid_features:
            raise tritonserver.InvalidArgumentError(
                f"Invalid features found: {invalid_features}. "
                "Each item in 'features' should be an instance of the tritonfrontend.Feature. "
                f"Valid options are: {[str(p) for p in Feature]}"
            )
        return features


class RestrictedFeatures:
    """
    Using `RestrictedFeatures` users can restrict access to certain features provided by
    the `KServeHttp` and `KServeGrpc` frontends. In order to use a restricted feature,
    the key-value pair ({`key`:`value`}) needs to be as a header with the request to the endpoint.
    Note: For the `KServeGrpc` endpoint, the header key needs a prefix of `triton-grpc-protocol-`
    when sending a request.

    Internally, the `RestrictedFeatures` class:
    - Stores collections of FeatureGroup instances
    - Checks for collisions for `Feature` instances among groups.
    - Serialize the data into a JSON string.

    Example:
        >>> from tritonfrontend import Feature, FeatureGroup, RestrictedFeatures
        >>> admin_group = FeatureGroup(key="admin-key", value="admin-value", features=[Feature.HEALTH, Feature.METADATA])
        >>> infer_group = FeatureGroup("infer-key", "infer-value", [Feature.INFERENCE])
        >>> rf = RestrictedFeatures([admin_group, infer_group])
        >>> rf.create_feature_group("trace-key", "trace-value", [Feature.TRACE])
        >>> rf
        [
            {"key": "admin-key", "value": "admin-value", "features": ["health", "metadata"]},
            {"key": "infer-key", "value": "infer-value", "features": ["inference"]},
            {"key": "trace-key", "value": "trace-value", "features": ["trace"]}
        ]
    """

    def __init__(self, groups: List[FeatureGroup] = []):
        self.feature_groups = []  # Stores FeatureGroup Instances
        self.features_restricted = set()  # Used for collision detection between groups

        for feat_group in groups:
            self.add_feature_group(feat_group)

    @handle_triton_error
    def add_feature_group(self, group: FeatureGroup) -> None:
        """
        Need to check for collision with features_restricted.
        If collision, raise InvalidArgumentError().
        If no collision, add group to feature_groups

        Example:
            >>> from tritonfrontend import Feature, FeatureGroup, RestrictedFeatures
            >>> health_group = FeatureGroup("health-key", "health-value", [Feature.HEALTH])
            >>> rf = RestrictedFeatures()
            >>> rf.add_feature_group(health_group)
            >>> rf
            [{"key": "health-key", "value": "health-value", "features": ["health"]}]
        """
        for feat in group.features:
            if feat in self.features_restricted:
                raise InvalidArgumentError(
                    "A given feature can only belong to one group."
                    f"{str(feat)} already belongs to an existing group."
                )

        self.features_restricted.update(group.features)
        self.feature_groups.append(group)

    @handle_triton_error
    def create_feature_group(
        self, key: str, value: str, features: List[Feature]
    ) -> None:
        """
        Factory method used to generate FeatureGroup instances and append them
        to the `RestrictedFeatures` object that invoked this function.

        Example:
            >>> from tritonfrontend import RestrictedFeatures, Feature
            >>> rf = RestrictedFeatures()
            >>> rf.create_feature_group("infer-key", "infer-value", [Feature.INFERENCE])
            >>> rf
            [{"key": "infer-key", "value": "infer-value", "features": ["inference"]}]
        """
        group = FeatureGroup(key, value, features)
        self.add_feature_group(group)

    @handle_triton_error
    def _gather_rest_data(self) -> dict:
        """
        Represents `RestrictedFeatures` Instance as a dictionary.
        Additionally, converts `Feature` instances to str equivalent.
        """
        # Dataclass_Instance.__dict__ provides shallow copy, so need a deep copy IF modifying
        rfeat_data = [
            deepcopy(feat_group.__dict__) for feat_group in self.feature_groups
        ]

        for idx in range(len(rfeat_data)):
            rfeat_data[idx]["features"] = [
                feat.value for feat in rfeat_data[idx]["features"]
            ]

        return rfeat_data

    def __str__(self) -> str:
        """
        A function to retrieve user-friendly string to view object contents.
        """
        return json.dumps(self._gather_rest_data(), indent=2)

    def __repr__(self) -> str:
        """
        A function to retrieve representation that has not been formatted.
        """
        return json.dumps(self._gather_rest_data())
