# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from absl.testing import parameterized
from neuralgcm.experimental import coordax
from neuralgcm.experimental.coordax import coordinate_systems
import numpy as np


class CoordinateSystemsTest(parameterized.TestCase):

  PRODUCT_XY = coordax.CartesianProduct(
      (coordax.NamedAxis('x', 2), coordax.NamedAxis('y', 3))
  )

  @parameterized.named_parameters(
      dict(
          testcase_name='empty',
          coordinates=(),
          expected=(),
      ),
      dict(
          testcase_name='single_other_axis',
          coordinates=(coordax.NamedAxis('x', 2),),
          expected=(coordax.NamedAxis('x', 2),),
      ),
      dict(
          testcase_name='single_selected_axis',
          coordinates=(
              coordax.SelectedAxis(coordax.NamedAxis('x', 2), axis=0),
          ),
          expected=(coordax.NamedAxis('x', 2),),
      ),
      dict(
          testcase_name='pair_of_other_axes',
          coordinates=(
              coordax.NamedAxis('x', 2),
              coordax.LabeledAxis('y', np.arange(3)),
          ),
          expected=(
              coordax.NamedAxis('x', 2),
              coordax.LabeledAxis('y', np.arange(3)),
          ),
      ),
      dict(
          testcase_name='pair_of_selections_correct',
          coordinates=(
              coordax.SelectedAxis(PRODUCT_XY, axis=0),
              coordax.SelectedAxis(PRODUCT_XY, axis=1),
          ),
          expected=(PRODUCT_XY,),
      ),
      dict(
          testcase_name='pair_of_selections_wrong_order',
          coordinates=(
              coordax.SelectedAxis(PRODUCT_XY, axis=1),
              coordax.SelectedAxis(PRODUCT_XY, axis=0),
          ),
          expected=(
              coordax.SelectedAxis(PRODUCT_XY, axis=1),
              coordax.SelectedAxis(PRODUCT_XY, axis=0),
          ),
      ),
      dict(
          testcase_name='selection_incomplete',
          coordinates=(coordax.SelectedAxis(PRODUCT_XY, axis=0),),
          expected=(coordax.SelectedAxis(PRODUCT_XY, axis=0),),
      ),
      dict(
          testcase_name='selections_with_following',
          coordinates=(
              coordax.SelectedAxis(PRODUCT_XY, axis=0),
              coordax.SelectedAxis(PRODUCT_XY, axis=1),
              coordax.NamedAxis('z', 4),
          ),
          expected=(
              PRODUCT_XY,
              coordax.NamedAxis('z', 4),
          ),
      ),
      dict(
          testcase_name='selections_with_preceeding',
          coordinates=(
              coordax.NamedAxis('z', 4),
              coordax.SelectedAxis(PRODUCT_XY, axis=0),
              coordax.SelectedAxis(PRODUCT_XY, axis=1),
          ),
          expected=(
              coordax.NamedAxis('z', 4),
              PRODUCT_XY,
          ),
      ),
      dict(
          testcase_name='selections_split',
          coordinates=(
              coordax.SelectedAxis(PRODUCT_XY, axis=0),
              coordax.NamedAxis('z', 4),
              coordax.SelectedAxis(PRODUCT_XY, axis=1),
          ),
          expected=(
              coordax.SelectedAxis(PRODUCT_XY, axis=0),
              coordax.NamedAxis('z', 4),
              coordax.SelectedAxis(PRODUCT_XY, axis=1),
          ),
      ),
      dict(
          testcase_name='two_selected_axes_consolidate_after',
          coordinates=(
              coordax.SelectedAxis(PRODUCT_XY, axis=0),
              coordax.SelectedAxis(coordax.NamedAxis('x', 4), axis=0),
          ),
          expected=(
              coordax.SelectedAxis(PRODUCT_XY, axis=0),
              coordax.NamedAxis('x', 4),
          ),
      ),
      dict(
          testcase_name='two_selected_axes_consolidate_before',
          coordinates=(
              coordax.SelectedAxis(coordax.NamedAxis('x', 4), axis=0),
              coordax.SelectedAxis(PRODUCT_XY, axis=0),
          ),
          expected=(
              coordax.NamedAxis('x', 4),
              coordax.SelectedAxis(PRODUCT_XY, axis=0),
          ),
      ),
  )
  def test_consolidate_coordinates(self, coordinates, expected):
    actual = coordinate_systems.consolidate_coordinates(*coordinates)
    self.assertEqual(actual, expected)

  @parameterized.named_parameters(
      dict(
          testcase_name='selected_axes_compoents_merge',
          coordinates=(
              coordax.SelectedAxis(PRODUCT_XY, axis=0),
              coordax.SelectedAxis(PRODUCT_XY, axis=1),
          ),
          expected=PRODUCT_XY,
      ),
      dict(
          testcase_name='selected_axis_simplified',
          coordinates=(
              coordax.SelectedAxis(coordax.NamedAxis('x', 4), axis=0),
              coordax.NamedAxis('z', 7),
          ),
          expected=coordax.CartesianProduct(
              (coordax.NamedAxis('x', 4), coordax.NamedAxis('z', 7))
          ),
      ),
      dict(
          testcase_name='cartesian_product_unraveled',
          coordinates=(
              coordax.NamedAxis('x', 7),
              coordax.CartesianProduct(
                  (coordax.NamedAxis('y', 7), coordax.NamedAxis('z', 4))
              ),
          ),
          expected=coordax.CartesianProduct((
              coordax.NamedAxis('x', 7),
              coordax.NamedAxis('y', 7),
              coordax.NamedAxis('z', 4),
          )),
      ),
      dict(
          testcase_name='consolidate_over_parts',
          coordinates=(
              coordax.SelectedAxis(PRODUCT_XY, axis=0),
              coordax.CartesianProduct((
                  coordax.SelectedAxis(PRODUCT_XY, axis=1),
                  coordax.NamedAxis('z', 4),
              )),
          ),
          expected=coordax.CartesianProduct((
              coordax.NamedAxis('x', 2),
              coordax.NamedAxis('y', 3),
              coordax.NamedAxis('z', 4),
          )),
      ),
  )
  def test_compose_coordinates(self, coordinates, expected):
    actual = coordinate_systems.compose_coordinates(*coordinates)
    self.assertEqual(actual, expected)
