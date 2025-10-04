# Copyright 2025 Google LLC
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

import re
from absl.testing import absltest
from absl.testing import parameterized
import chex
import coordax as cx
import jax
from neuralgcm.experimental.core import field_utils
import numpy as np


class SplitToFieldsTest(parameterized.TestCase):
  """Tests that split_to_fields works as expected."""

  def test_split_to_fields_flat_out(self):
    b, x = cx.SizedAxis('batch', 3), cx.LabeledAxis('x', np.array([0, 1]))
    field = cx.wrap(np.arange(6).reshape(3, 2), b, x)
    targets = {'a': x, 'b': x, 'c': x}
    expected = {
        'a': cx.wrap(np.array([0, 1]), x),
        'b': cx.wrap(np.array([2, 3]), x),
        'c': cx.wrap(np.array([4, 5]), x),
    }
    actual = field_utils.split_to_fields(field, targets)
    chex.assert_trees_all_close(actual, expected)

  def test_split_to_fields_mixed_out(self):
    b, x = cx.SizedAxis('batch', 6), cx.LabeledAxis('x', np.array([0, 1]))
    s, d = cx.SizedAxis('s', 2), cx.SizedAxis('d', 3)
    field = cx.wrap(np.arange(12).reshape(6, 2), b, x)
    targets = {
        'a': x,  # takes size 1.
        'b': cx.compose_coordinates(s, x),  # takes size 2.
        'c': cx.compose_coordinates(d, x),  # takes size 3.
    }
    expected = {
        'a': cx.wrap(np.array([0, 1]), x),
        'b': cx.wrap(np.array([[2, 3], [4, 5]]), s, x),
        'c': cx.wrap(np.array([[6, 7], [8, 9], [10, 11]]), d, x),
    }
    actual = field_utils.split_to_fields(field, targets)
    chex.assert_trees_all_close(actual, expected)

  def test_split_to_fields_multi_dims(self):
    xy = cx.compose_coordinates(cx.SizedAxis('x', 6), cx.SizedAxis('y', 7))
    field = cx.wrap(np.ones((5,) + xy.shape), None, xy)
    s = cx.SizedAxis('s', 2)
    sxy = cx.compose_coordinates(s, xy)
    targets = {'a': xy, 'b': sxy, 'c': sxy}
    expected = {
        'a': cx.wrap(np.ones(xy.shape), xy),
        'b': cx.wrap(np.ones(sxy.shape), sxy),
        'c': cx.wrap(np.ones(sxy.shape), sxy),
    }
    actual = field_utils.split_to_fields(field, targets)
    chex.assert_trees_all_close(actual, expected)

  def test_split_to_fields_aligns_outputs(self):
    x, y = cx.SizedAxis('x', 6), cx.SizedAxis('y', 7)
    field = cx.wrap(np.ones((3,) + x.shape + y.shape), None, x, y)
    yx = cx.compose_coordinates(x, y)
    targets = {'a': yx, 'b': yx, 'c': yx}  # requests transposed xy;
    expected = {
        'a': cx.wrap(np.ones(yx.shape), yx),
        'b': cx.wrap(np.ones(yx.shape), yx),
        'c': cx.wrap(np.ones(yx.shape), yx),
    }
    actual = field_utils.split_to_fields(field, targets)
    chex.assert_trees_all_close(actual, expected)

  def test_split_to_fields_raises_on_misaligned_coords(self):
    """Tests that split_to_fields raises on misaligned coordinates."""
    x, y = cx.LabeledAxis('x', np.arange(3)), cx.LabeledAxis('y', np.arange(2))
    xy = cx.compose_coordinates(x, y)
    field = cx.wrap(np.ones((2,) + xy.shape), None, xy)
    good_targets = {'a': xy, 'b': xy}  # should not raise
    _ = field_utils.split_to_fields(field, good_targets)
    bad_xy = cx.compose_coordinates(cx.SizedAxis('x', 3), cx.SizedAxis('y', 2))
    bad_targets = {'a': bad_xy, 'b': bad_xy}
    with self.assertRaisesRegex(
        ValueError,
        re.escape(
            r'does not specify a valid split element because it is not aligned '
            'with the non-split part of input field'
        ),
    ):
      field_utils.split_to_fields(field, bad_targets)

  def test_split_to_fields_raises_on_wrong_split_size(self):
    """Tests that split_to_fields raises on wrong split size."""
    b, x = cx.SizedAxis('batch', 3), cx.LabeledAxis('x', np.array([0, 1]))
    field = cx.wrap(np.arange(6).reshape(3, 2), b, x)
    targets = {'a': x, 'b': x}  # requests 2*2 != 3*2.
    with self.assertRaisesWithLiteralMatch(
        ValueError,
        'The total size of the dimensions defined in `targets` (2)'
        ' does not match the size of the dimension being split in the input'
        ' field (3).',
    ):
      field_utils.split_to_fields(field, targets)

  def test_split_to_fields_raises_if_too_many_new_dims(self):
    """Tests that split_to_fields raises if more than 1 new dim is detected."""
    b, x = cx.SizedAxis('batch', 2), cx.LabeledAxis('x', np.arange(7))
    field = cx.wrap(np.zeros((2, 7)), b, x)
    d = cx.SizedAxis('d', 1)
    targets = {
        'a': cx.compose_coordinates(d, x),
        'b': cx.compose_coordinates(d, cx.SizedAxis('second_new', 1), x),
    }
    with self.assertRaisesRegex(
        ValueError,
        re.escape(r'has more than 1 new axis compared to input field'),
    ):
      field_utils.split_to_fields(field, targets)


class CombineFieldsTest(parameterized.TestCase):
  """Tests that combine_fields works as expected."""

  def test_combine_fields_supports_mixed_concat_axes(self):
    x, y = cx.SizedAxis('x', 3), cx.SizedAxis('y', 5)
    z, level = cx.SizedAxis('z', 2), cx.SizedAxis('level', 7)
    fields = {  # concatenates z and level when aligned on (x, y).
        'a': cx.wrap(np.ones((2, 3, 5)), z, x, y),
        'b': cx.wrap(np.ones((7, 3, 5)), level, x, y),
        'c': cx.wrap(np.ones((7, 3, 5)), level, x, y),
    }
    actual = field_utils.combine_fields(fields, dims_to_align=(x, y))
    expected = cx.wrap(np.ones((7 + 7 + 2, 3, 5)), None, x, y)
    chex.assert_trees_all_close(actual, expected)

  def test_combine_fields_supports_missing_concat_axis(self):
    x, y = cx.SizedAxis('x', 3), cx.SizedAxis('y', 5)
    level = cx.SizedAxis('level', 7)
    fields = {
        'a_surf': cx.wrap(np.ones((3, 5)), x, y),  # should expand as (1, 3, 5).
        'b': cx.wrap(np.ones((7, 3, 5)), level, x, y),
        'c': cx.wrap(np.ones((7, 3, 5)), level, x, y),
    }
    actual = field_utils.combine_fields(fields, dims_to_align=(x, y))
    expected = cx.wrap(np.ones((1 + 7 + 7, 3, 5)), None, x, y)
    chex.assert_trees_all_close(actual, expected)

  def test_combine_fields_works_as_stack(self):
    x, y = cx.SizedAxis('x', 3), cx.SizedAxis('y', 5)
    level = cx.SizedAxis('level', 7)
    fields = {  # all expand to (1, 7, 3, 5) since aligned on level, x, y.
        'a': cx.wrap(np.ones((7, 3, 5)), level, x, y),
        'b': cx.wrap(np.ones((7, 3, 5)), level, x, y),
        'c': cx.wrap(np.ones((7, 3, 5)), level, x, y),
    }
    actual = field_utils.combine_fields(fields, dims_to_align=(level, x, y))
    expected = cx.wrap(np.ones((3, 7, 3, 5)), None, level, x, y)
    chex.assert_trees_all_close(actual, expected)

  def test_combine_fields_out_axis_tag(self):
    x = cx.SizedAxis('x', 5)
    fields = {'a': cx.wrap(np.ones(5), x), 'b': cx.wrap(np.ones(5), x)}

    with self.subTest('coordinate_out_tag'):
      out_tag = cx.SizedAxis('out', 2)  # 2
      actual = field_utils.combine_fields(fields, (x,), out_tag)
      expected = cx.wrap(np.ones((2, 5)), out_tag, x)
      chex.assert_trees_all_close(actual, expected)

    with self.subTest('name_out_tag'):
      actual = field_utils.combine_fields(fields, (x,), 'out')
      expected = cx.wrap(np.ones((2, 5)), 'out', x)
      chex.assert_trees_all_close(actual, expected)

  def test_combine_fields_supports_dims_and_coords(self):
    x, y = cx.SizedAxis('x', 3), cx.SizedAxis('y', 5)
    z, level = cx.SizedAxis('z', 2), cx.SizedAxis('level', 7)
    fields = {
        'a': cx.wrap(np.ones((2, 7, 3, 5)), z, level, x, y),
        'b': cx.wrap(np.ones((7, 3, 5)), level, x, y),
        'c': cx.wrap(np.ones((7, 3, 5)), level, x, y),
    }
    xy = cx.compose_coordinates(x, y)  # can pass coords as dims_to_align.
    actual = field_utils.combine_fields(fields, dims_to_align=('level', xy))
    expected = cx.wrap(np.ones((4, 7, 3, 5)), None, level, x, y)
    chex.assert_trees_all_close(actual, expected)

  def test_combine_fields_raises_on_repeated_dims_to_align(self):
    x = cx.SizedAxis('x', 3)
    with self.assertRaisesWithLiteralMatch(
        ValueError,
        "`dims_to_align` must be unique, but got repeated_dims=['x'].",
    ):
      field_utils.combine_fields({}, dims_to_align=('x', x))

  def test_combine_fields_raises_on_too_many_new_axes(self):
    x, y = cx.SizedAxis('x', 3), cx.SizedAxis('y', 5)
    z, level = cx.SizedAxis('z', 2), cx.SizedAxis('level', 7)
    fields = {
        'two_new': cx.wrap(np.ones((2, 7, 3, 5)), z, level, x, y),
        'no_new': cx.wrap(np.ones((3, 5)), x, y),
    }
    with self.assertRaisesWithLiteralMatch(
        ValueError,
        f"Field {fields['two_new']} has more than 1 axis other than"
        " aligned_dims_and_axes=('x', 'y').",
    ):
      field_utils.combine_fields(fields, dims_to_align=('x', 'y'))

  def test_combine_fields_raises_on_missing_alignment_dim(self):
    x, y = cx.SizedAxis('x', 3), cx.SizedAxis('y', 5)
    level = cx.SizedAxis('level', 7)
    fields = {
        'missing_x': cx.wrap(np.ones((7, 5)), level, y),
        'valid': cx.wrap(np.ones((3, 5)), x, y),
    }
    with self.assertRaisesWithLiteralMatch(
        ValueError,
        f"Cannot combine {fields['missing_x']} because it does not align with"
        " ('x', 'y')",
    ):
      field_utils.combine_fields(fields, dims_to_align=('x', 'y'))

  def test_combine_fields_no_unique_axis_order(self):
    x, y = cx.SizedAxis('x', 3), cx.SizedAxis('y', 5)
    fields = {
        'y_then_x': cx.wrap(np.ones((7, 5, 3)), 'l', y, x),
        'x_then_y': cx.wrap(np.ones((3, 5)), x, y),
    }
    with self.assertRaisesRegex(
        ValueError,
        re.escape('No unique out_axes found in inputs'),
    ):
      field_utils.combine_fields(fields, dims_to_align=('x', 'y'))


class UtilsTest(parameterized.TestCase):

  def test_shape_struct_fields_from_coords(self):
    coords = {
        'a': cx.LabeledAxis('a', np.array([1, 2, 3])),
        'b': cx.LabeledAxis('b', np.array([4, 5, 6])),
    }
    actual = field_utils.shape_struct_fields_from_coords(coords)
    self.assertEqual(cx.get_coordinate(actual['a']), coords['a'])
    self.assertEqual(cx.get_coordinate(actual['b']), coords['b'])
    self.assertIsInstance(actual['a'].data, jax.ShapeDtypeStruct)


class InAxesUtilTest(parameterized.TestCase):
  """Tests in_axes utility functions."""

  def test_in_axes_for_coord(self):
    x, y = cx.SizedAxis('x', 3), cx.SizedAxis('y', 4)
    f1 = cx.wrap(np.zeros((3, 4)), x, y)
    f2 = cx.wrap(np.zeros((4, 3)), y, x)
    f3 = cx.wrap(np.zeros((4,)), y)
    inputs = {'a': f1, 'b': (f2, 123, f3)}
    with self.subTest('map_over_x'):
      actual = field_utils.in_axes_for_coord(inputs, x)
      expected = {'a': 0, 'b': (1, None, None)}
      chex.assert_trees_all_equal(actual, expected)
    with self.subTest('map_over_y'):
      actual = field_utils.in_axes_for_coord(inputs, y)
      expected = {'a': 1, 'b': (0, None, 0)}
      chex.assert_trees_all_equal(actual, expected)

  def test_in_axes_for_coord_raises_for_non_1d_coord(self):
    x, y = cx.SizedAxis('x', 3), cx.SizedAxis('y', 4)
    f1 = cx.wrap(np.zeros((3, 4)), x, y)
    xy = cx.compose_coordinates(x, y)
    with self.assertRaisesRegex(ValueError, 'idx can be computed only for 1d'):
      field_utils.in_axes_for_coord(f1, xy)

  def test_in_axes_for_coord_with_nesting(self):
    x, y = cx.SizedAxis('x', 3), cx.SizedAxis('y', 4)
    f = {
        'a': cx.wrap(np.zeros((3, 4)), x, y),
        'b': cx.wrap(np.zeros((4, 3)), y, x),
    }
    actual_outer, actual_inner = field_utils.in_axes_for_coord(f, [x, y])
    expected_outer = {'a': 0, 'b': 1}
    expected_inner = {'a': 0, 'b': 0}
    chex.assert_trees_all_equal(actual_outer, expected_outer)
    chex.assert_trees_all_equal(actual_inner, expected_inner)

  @parameterized.named_parameters(
      dict(
          testcase_name='one_level',
          in_axes=((0, 1),),
          expected=((0, 1),),
      ),
      dict(
          testcase_name='two_levels_no_shift',
          in_axes=((1, 1), (0, 0)),
          expected=((1, 1), (0, 0)),
      ),
      dict(
          testcase_name='two_levels_with_shift',
          in_axes=({'a': 0, 'b': 1}, {'a': 1, 'b': 0}),
          expected=({'a': 0, 'b': 1}, {'a': 0, 'b': 0}),
      ),
      dict(
          testcase_name='three_levels_with_shifts_and_none',
          in_axes=((1, None, 2), (0, 1, 0), (2, 0, 1)),
          expected=(
              (1, None, 2),
              (0, 1, 0),
              (0, 0, 0),
          ),
      ),
      dict(
          testcase_name='two_levels_not_leading_axes',
          in_axes=((0, 2), (1, 0)),
          expected=((0, 2), (0, 0)),
      ),
      dict(
          testcase_name='nested_dict_structure',
          in_axes=({'a': {'b': 0}}, {'a': {'b': 1}}),
          expected=({'a': {'b': 0}}, {'a': {'b': 0}}),
      ),
      dict(
          testcase_name='repeated_none',
          in_axes=((None, 0), (None, 1)),
          expected=((None, 0), (None, 0)),
      ),
  )
  def test_nest_in_axes(self, in_axes, expected):
    actual = field_utils.nest_in_axes(*in_axes)
    chex.assert_trees_all_equal(actual, expected)

  def test_nest_in_axes_raises_on_negative_axis(self):
    with self.assertRaisesRegex(ValueError, 'Negative axes are not allowed'):
      field_utils.nest_in_axes((-1, 0))

  def test_nest_in_axes_raises_on_repeated_axis(self):
    with self.assertRaisesRegex(
        ValueError,
        'leaf in.*is mapped over the same axis multiple times',
    ):
      field_utils.nest_in_axes((0, 1), (0, 2))


if __name__ == '__main__':
  absltest.main()
