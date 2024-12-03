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
import re
import textwrap

from absl.testing import absltest
import jax
from neuralgcm.experimental.coordax import named_axes
import numpy as np


def assert_named_array_equal(
    actual: named_axes.NamedArray,
    expected: named_axes.NamedArray,
) -> None:
  """Asserts that a NamedArray has the expected data and dims."""
  np.testing.assert_array_equal(actual.data, expected.data)
  assert actual.dims == expected.dims, (expected.dims, actual.dims)


class NamedAxesTest(absltest.TestCase):

  def test_named_array(self):
    data = np.arange(10).reshape((2, 5))
    array = named_axes.NamedArray(data, ('x', None))
    self.assertEqual(array.dims, ('x', None))
    np.testing.assert_array_equal(array.data, data)
    self.assertEqual(array.ndim, 2)
    self.assertEqual(array.shape, (2, 5))
    self.assertEqual(array.positional_shape, (5,))
    self.assertEqual(array.named_shape, {'x': 2})
    self.assertEqual(
        repr(array),
        textwrap.dedent("""\
            NamedArray(
                data=Array([[0, 1, 2, 3, 4],
                            [5, 6, 7, 8, 9]], dtype=int32),
                dims=('x', None),
            )"""),
    )

  def test_constructor_error(self):
    with self.assertRaisesRegex(
        ValueError, re.escape(r'data.ndim=2 != len(dims)=1')
    ):
      named_axes.NamedArray(np.zeros((2, 5)), ('x',))
    with self.assertRaisesRegex(
        ValueError, re.escape(r'dimension names may not be repeated')
    ):
      named_axes.NamedArray(np.zeros((2, 5)), ('x', 'x'))

  def test_tree_map_same_dims(self):
    data = np.arange(10).reshape((2, 5))
    array = named_axes.NamedArray(data, ('x', 'y'))
    actual = jax.tree.map(lambda x: x, array)
    assert_named_array_equal(actual, array)

  def test_tree_map_cannot_trim(self):
    data = np.arange(10).reshape((2, 5))
    array = named_axes.NamedArray(data, ('x', 'y'))
    with self.assertRaisesRegex(
        ValueError,
        re.escape(
            'cannot trim named dimensions when unflattening to a NamedArray:'
            " ('x',)."
        ),
    ):
      jax.tree.map(lambda x: x[0, :], array)

  def test_tree_map_wrong_dim_size(self):
    data = np.arange(10).reshape((2, 5))
    array = named_axes.NamedArray(data, ('x', 'y'))
    with self.assertRaisesRegex(
        ValueError,
        re.escape(
            'named shape mismatch when unflattening to a NamedArray: '
            "{'x': 2, 'y': 3} != {'x': 2, 'y': 5}."
        ),
    ):
      jax.tree.map(lambda x: x[:, :3], array)

  def test_tree_map_new_dim(self):
    data = np.arange(10).reshape((2, 5))
    array = named_axes.NamedArray(data, ('x', 'y'))
    expected = named_axes.NamedArray(data[np.newaxis, ...], (None, 'x', 'y'))
    actual = jax.tree.map(lambda x: x[np.newaxis, ...], array)
    assert_named_array_equal(actual, expected)

  def test_tree_map_trim_dim(self):
    data = np.arange(10).reshape((2, 5))
    array = named_axes.NamedArray(data, (None, 'y'))
    expected = named_axes.NamedArray(data[0, ...], ('y',))
    actual = jax.tree.map(lambda x: x[0, ...], array)
    assert_named_array_equal(actual, expected)

  def test_jit(self):
    data = np.arange(10).reshape((2, 5))
    array = named_axes.NamedArray(data, ('x', 'y'))
    actual = jax.jit(lambda x: x)(array)
    assert_named_array_equal(actual, array)

  def test_vmap(self):
    data = np.arange(10).reshape((2, 5))
    array = named_axes.NamedArray(data, (None, 'y'))
    actual = jax.vmap(lambda x: x)(array)
    assert_named_array_equal(actual, array)

    array = named_axes.NamedArray(data, ('x', 'y'))
    with self.assertRaisesRegex(
        ValueError,
        re.escape(
            'If you are using vmap or scan, the first dimension must be'
            ' unnamed.'
        ),
    ):
      jax.vmap(lambda x: x)(array)

  def test_scan(self):
    data = np.arange(10).reshape((2, 5))
    array = named_axes.NamedArray(data, (None, 'y'))
    _, actual = jax.lax.scan(lambda _, x: (None, x), init=None, xs=array)
    assert_named_array_equal(actual, array)

  def test_tag_valid(self):
    data = np.arange(10).reshape((2, 5))

    array = named_axes.NamedArray(data, (None, 'y'))
    expected = named_axes.NamedArray(data, ('x', 'y'))
    actual = array.tag('x')
    assert_named_array_equal(actual, expected)

    array = named_axes.NamedArray(data, (None, None))
    expected = named_axes.NamedArray(data, ('x', 'y'))
    actual = array.tag('x', 'y')
    assert_named_array_equal(actual, expected)

  def test_tag_errors(self):
    data = np.arange(10).reshape((2, 5))

    array = named_axes.NamedArray(data, (None, 'y'))
    with self.assertRaisesRegex(
        ValueError,
        re.escape(
            'there must be exactly as many dimensions given to `tag` as there'
            ' are positional axes in the array, but got () for '
            '1 positional axis.'
        ),
    ):
      array.tag()

    array = named_axes.NamedArray(data, (None, None))
    with self.assertRaisesRegex(
        ValueError,
        re.escape(
            'there must be exactly as many dimensions given to `tag` as there'
            " are positional axes in the array, but got ('x',) for "
            '2 positional axes.'
        ),
    ):
      array.tag('x')

    with self.assertRaisesRegex(
        TypeError,
        re.escape('dimension names must be strings: (None, None)'),
    ):
      array.tag(None, None)

  def test_untag_valid(self):
    data = np.arange(10).reshape((2, 5))
    array = named_axes.NamedArray(data, ('x', 'y'))

    expected = named_axes.NamedArray(data, (None, 'y'))
    actual = array.untag('x')
    assert_named_array_equal(actual, expected)

    expected = named_axes.NamedArray(data, ('x', None))
    actual = array.untag('y')
    assert_named_array_equal(actual, expected)

    expected = named_axes.NamedArray(data, (None, None))
    actual = array.untag('x', 'y')
    assert_named_array_equal(actual, expected)

  def test_untag_invalid(self):
    data = np.arange(10).reshape((2, 5))
    partially_named_array = named_axes.NamedArray(data, (None, 'y'))
    fully_named_array = named_axes.NamedArray(data, ('x', 'y'))

    with self.assertRaisesRegex(
        ValueError,
        re.escape(
            '`untag` cannot be used to introduce positional axes for a'
            ' NamedArray that already has positional axes. Please assign names'
            ' to the existing positional axes first using `tag`.'
        ),
    ):
      partially_named_array.untag('y')

    with self.assertRaisesRegex(
        ValueError,
        re.escape(
            "cannot untag ('invalid',) because they are not a subset of the"
            " current named dimensions ('x', 'y')"
        ),
    ):
      fully_named_array.untag('invalid')

    with self.assertRaisesRegex(
        ValueError,
        re.escape(
            "cannot untag ('y', 'x') because they do not appear in the order of"
            " the current named dimensions ('x', 'y')"
        ),
    ):
      fully_named_array.untag('y', 'x')


if __name__ == '__main__':
  absltest.main()
