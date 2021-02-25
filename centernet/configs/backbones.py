# Lint as: python3
# Copyright 2020 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""Backbones configurations."""
from typing import List

# Import libraries
import dataclasses

from official.modeling import hyperparams


@dataclasses.dataclass
class Hourglass(hyperparams.Config):
  """Hourglass config."""
  input_channel_dims: int = 128
  channel_dims_per_stage: List[int] = dataclasses.field(
      default_factory=lambda: [256, 256, 384, 384, 384, 512])
  blocks_per_stage: List[int] = dataclasses.field(
      default_factory=lambda: [2, 2, 2, 2, 2, 4])
  num_hourglasses: int = 2
  initial_downsample: bool = True
