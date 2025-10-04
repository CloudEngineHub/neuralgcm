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

"""Utilities for manipulating and transforming modules."""

from __future__ import annotations

import dataclasses
import functools
import itertools
import operator
from typing import Iterable, NamedTuple, Sequence, overload

import chex
import coordax as cx
from flax import nnx
import jax
from neuralgcm.experimental.core import field_utils
from neuralgcm.experimental.core import pytree_utils
from neuralgcm.experimental.core import typing


def ensure_unchanged_state_structure(
    method=None, *, excluded_dims: Sequence[str] | None = None
):
  """Wraps `method` of a nnx.Module checking that pytree struct is unchanged.

  The check is performed by comparing the coordinate structure of the module
  state before and after calling the `method`. Coordinates with dimension names
  in excluded_dims are only checked for existence by squeezing coordinates to
  size 1. This enables checks on methods where a subset of dimensions may
  change shape, e.g. updating dynamic state of the model.

  Args:
    method: The method to wrap. If None, works as a decorator.
    excluded_dims: Dimensions to exclude from the coordinate structure check.

  Returns:
    The wrapped method or a decorator.
  """
  excluded_dims = excluded_dims or []

  if method is None:
    return functools.partial(
        ensure_unchanged_state_structure, excluded_dims=excluded_dims
    )

  def _get_coord_struct(pytree: typing.Pytree) -> typing.Pytree:
    is_coord = lambda x: isinstance(x, cx.Coordinate)
    to_coord = lambda c: c.coordinate if cx.is_field(c) else c

    def squeeze_excluded(c: cx.Coordinate) -> cx.Coordinate:
      if not is_coord(c):
        return c
      axes = [
          cx.DummyAxis(ax.dims[0], 1) if ax.dims[0] in excluded_dims else ax
          for ax in c.axes
      ]
      return cx.compose_coordinates(*axes)

    field_struct = pytree_utils.shape_structure(pytree)
    coord_struct = jax.tree.map(to_coord, field_struct, is_leaf=cx.is_field)
    coord_struct = jax.tree.map(
        squeeze_excluded, coord_struct, is_leaf=is_coord
    )
    return coord_struct

  @functools.wraps(method)
  def wrapper(module: nnx.Module, *args, **kwargs):
    if not isinstance(module, nnx.Module):
      raise TypeError(
          '`ensure_unchanged_state_structure` must wrap an nnx.Module method'
      )
    graph_def_before, state_before = nnx.split(module)
    state_before = _get_coord_struct(state_before)
    result = method(module, *args, **kwargs)  # runs the method.
    graph_def_after, state_after = nnx.split(module)
    state_after = _get_coord_struct(state_after)
    if graph_def_after != graph_def_before:
      raise ValueError(
          f'GraphDef changed: {graph_def_before=} {graph_def_after=}'
      )
    try:
      chex.assert_trees_all_equal_shapes_and_dtypes(state_before, state_after)
    except (AssertionError, ValueError) as e:
      raise ValueError(
          'change in the pytree structure detected while running'
          f' "{method.__name__}":\n{e}'
      ) from e
    return result

  return wrapper


def vectorize_module(
    module: nnx.Module,
    vectorization_specs: dict[nnx.filterlib.Filter, cx.Coordinate],
) -> None:
  """Vectorizes the state of a `module` in place using `vectorization_specs`."""

  def broadcast(x: cx.Field, coord: cx.Coordinate) -> cx.Field:
    if not cx.is_field(x):
      raise ValueError(
          'module state vectorization requires Field variables, but'
          f' encountered {type(x)=}'
      )
    return x.broadcast_like(cx.compose_coordinates(coord, x.coordinate))

  for k, coord in vectorization_specs.items():
    k_state = jax.tree.map(
        functools.partial(broadcast, coord=coord),
        nnx.state(module, k),
        is_leaf=cx.is_field,
    )
    nnx.update(module, k_state)


def untag_module_state(
    module: nnx.Module,
    coordinate: cx.Coordinate,
    vectorized_axes: dict[nnx.filterlib.Filter, cx.Coordinate],
) -> None:
  """Untags axes of `coordinate` from the state of the `module`."""
  vectorized_axes_set = functools.reduce(
      operator.or_, (set(v.axes) for v in vectorized_axes.values())
  )
  if any(ax not in vectorized_axes_set for ax in coordinate.axes):
    raise ValueError(
        f'untag_module_state got {coordinate=} with axis that is not present '
        f'anywhere in {vectorized_axes=}'
    )
  for state_filter, coord in vectorized_axes.items():
    untag_components = [ax for ax in coordinate.axes if ax in coord.axes]
    if untag_components:
      untag_axis = cx.compose_coordinates(*untag_components)
      state_to_untag = nnx.state(module, state_filter)
      nnx.update(module, cx.untag(state_to_untag, untag_axis))


def tag_module_state(
    module: nnx.Module,
    coordinate: cx.Coordinate,
    vectorized_axes: dict[nnx.filterlib.Filter, cx.Coordinate],
) -> None:
  """Tags axes of `coordinate` to the state of the `module`."""
  vectorized_axes_set = functools.reduce(
      operator.or_, (set(v.axes) for v in vectorized_axes.values())
  )
  if any(ax not in vectorized_axes_set for ax in coordinate.axes):
    raise ValueError(
        f'tag_module_state got {coordinate=} with axis that is not present '
        f'anywhere in {vectorized_axes=}'
    )
  for state_filter, coord in vectorized_axes.items():
    tag_components = [ax for ax in coordinate.axes if ax in coord.axes]
    if tag_components:
      tag_axis = cx.compose_coordinates(*tag_components)
      state_to_untag = nnx.state(module, state_filter)
      nnx.update(module, cx.tag(state_to_untag, tag_axis))


def _are_certainly_disjoint_predicates(
    p1: nnx.Predicate, p2: nnx.Predicate
) -> bool:
  """Returns True if we can guarantee that two predicates are disjoint."""
  # Note this implementation assumes deconstruction of p1, some cases can be
  # handled by considering disjointness of (p2, p1).
  if isinstance(p1, nnx.filterlib.Nothing):  # Handle Nothing.
    return True

  if isinstance(p1, nnx.filterlib.Everything):  # Handle Everything.
    return isinstance(p2, nnx.filterlib.Nothing)

  if isinstance(p1, nnx.filterlib.Not):  # Handle Not.
    if p1.predicate == p2:
      return True

  if isinstance(p1, nnx.filterlib.Any):  # Handle Any.
    return all(
        _are_certainly_disjoint_filters(sub_p, p2) for sub_p in p1.predicates
    )

  if isinstance(p1, nnx.filterlib.All):  # Handle All
    return any(
        _are_certainly_disjoint_filters(sub_p, p2) for sub_p in p1.predicates
    )

  if isinstance(p1, nnx.filterlib.OfType):
    if isinstance(p2, nnx.filterlib.OfType):
      t1, t2 = p1.type, p2.type  # check if filters are in subclass relation.
      if not issubclass(t1, t2) and not issubclass(t2, t1):
        return True

  if isinstance(p1, nnx.filterlib.WithTag):  # Handle WithTag.
    if isinstance(p2, nnx.filterlib.WithTag):
      if p1.tag != p2.tag:
        return True
  # Other cases are hard to check, so we conservatively return False.
  return False


def _is_certainly_subset_predicate(
    p1: nnx.Predicate, p2: nnx.Predicate
) -> bool:
  """Returns True if we can guarantee that p1 is a subset of p2."""
  if isinstance(p2, nnx.filterlib.Everything):
    return True
  if isinstance(p1, nnx.filterlib.Nothing):
    return True
  if p1 == p2:
    return True

  if isinstance(p1, nnx.filterlib.OfType) and isinstance(
      p2, nnx.filterlib.OfType
  ):
    if issubclass(p1.type, p2.type):
      return True

  if isinstance(p1, nnx.filterlib.Any):
    return all(
        _is_certainly_subset_predicate(sub_p, p2) for sub_p in p1.predicates
    )

  if isinstance(p2, nnx.filterlib.Any):
    return any(
        _is_certainly_subset_predicate(p1, sub_p) for sub_p in p2.predicates
    )

  if isinstance(p1, nnx.filterlib.All):
    return any(
        _is_certainly_subset_predicate(sub_p, p2) for sub_p in p1.predicates
    )

  if isinstance(p2, nnx.filterlib.All):
    return all(
        _is_certainly_subset_predicate(p1, sub_p) for sub_p in p2.predicates
    )

  if isinstance(p1, nnx.filterlib.Not) and isinstance(p2, nnx.filterlib.Not):
    return _is_certainly_subset_predicate(p2.predicate, p1.predicate)

  return False


def _are_certainly_disjoint_filters(
    filter_a: nnx.filterlib.Filter, filter_b: nnx.filterlib.Filter
) -> bool:
  """Returns True if two filters can be guaranteed to be disjoint.

  Two filters are disjoint if there is no variable that can be matched by both.
  This function provides a best-effort check based on the filter types.
  It cannot prove disjointness for arbitrary callable filters.

  Args:
    filter_a: The first filter.
    filter_b: The second filter.

  Returns:
    True if the check determines that the filters are disjoint, False otherwise.
  """
  p1 = nnx.filterlib.to_predicate(filter_a)
  p2 = nnx.filterlib.to_predicate(filter_b)
  ab_direction_disjoint = _are_certainly_disjoint_predicates(p1, p2)
  ba_direction_disjoint = _are_certainly_disjoint_predicates(p2, p1)
  return ab_direction_disjoint or ba_direction_disjoint


def is_filter_subset(
    f: nnx.filterlib.Filter, filter_group: nnx.filterlib.Filter
) -> bool:
  """Returns True if `f` can be guaranteed to be a subset of `filter_group`."""
  p1 = nnx.filterlib.to_predicate(f)
  p2 = nnx.filterlib.to_predicate(filter_group)
  return _is_certainly_subset_predicate(p1, p2)


def merge_vectorized_axes(
    vectorized_axes_head: dict[nnx.filterlib.Filter, cx.Coordinate],
    vectorized_axes_tail: dict[nnx.filterlib.Filter, cx.Coordinate],
) -> dict[nnx.filterlib.Filter, cx.Coordinate]:
  """Returns merged vectorized axes with head specifying leading dimensions."""
  head = vectorized_axes_head.copy()
  tail = vectorized_axes_tail.copy()
  head_ellipsis_axes = head.pop(..., cx.Scalar())
  tail_ellipsis_axes = tail.pop(..., cx.Scalar())
  # Split keys into common and differences
  head_keys = set(head.keys())
  tail_keys = set(tail.keys())
  common_keys = head_keys.intersection(tail_keys)
  diff_head_keys = head_keys.difference(tail_keys)
  diff_tail_keys = tail_keys.difference(head_keys)
  merged = {k: cx.compose_coordinates(head[k], tail[k]) for k in common_keys}
  diff_head = {
      k: cx.compose_coordinates(head[k], tail_ellipsis_axes)
      for k in diff_head_keys
  }
  diff_tail = {
      k: cx.compose_coordinates(head_ellipsis_axes, tail[k])
      for k in diff_tail_keys
  }
  if not all(
      _are_certainly_disjoint_filters(k1, k2)
      for k1, k2 in itertools.product(diff_head_keys, diff_tail_keys)
  ):
    potentially_overlapping = next(
        (k1, k2)
        for k1, k2 in itertools.product(diff_head_keys, diff_tail_keys)
        if not _are_certainly_disjoint_filters(k1, k2)
    )
    raise ValueError(
        'Cannot merge vectorized axes with potentially overlapping filters: '
        f'{potentially_overlapping[0]!r} and {potentially_overlapping[1]!r}.'
    )
  merged.update(diff_head)
  merged.update(diff_tail)
  combined_ellipsis = cx.compose_coordinates(
      head_ellipsis_axes, tail_ellipsis_axes
  )
  # Add ellipsis back if we had it in either set of filters.
  if ... in vectorized_axes_head or ... in vectorized_axes_tail:
    merged[...] = combined_ellipsis

  return merged


@overload
def state_in_axes_for_coord(
    vectorized_axes: dict[nnx.filterlib.Filter, cx.Coordinate],
    coord: cx.Coordinate,
) -> nnx.StateAxes:
  ...


@overload
def state_in_axes_for_coord(
    vectorized_axes: dict[nnx.filterlib.Filter, cx.Coordinate],
    coord: Sequence[cx.Coordinate],
) -> Sequence[nnx.StateAxes]:
  ...


def state_in_axes_for_coord(
    vectorized_axes: dict[nnx.filterlib.Filter, cx.Coordinate],
    coord: cx.Coordinate | Sequence[cx.Coordinate],
) -> nnx.StateAxes | Sequence[nnx.StateAxes]:
  if isinstance(coord, Sequence):
    return nest_state_in_axes(
        *(state_in_axes_for_coord(vectorized_axes, c) for c in coord)
    )
  dummy = {k: cx.shape_struct_field(v) for k, v in vectorized_axes.items()}
  axes = {k: field_utils.in_axes_for_coord(v, coord) for k, v in dummy.items()}
  return nnx.StateAxes(axes)


def nest_state_in_axes(
    *state_axes_to_nest: nnx.StateAxes,
) -> tuple[nnx.StateAxes, ...]:
  """Returns `state_axes_to_nest` adjusted for vmap nesting from outer to inner.

  Args:
    *state_axes_to_nest: A sequence of `nnx.StateAxes` with equal keys
      representing `vmap` indices from outermost to innermost.

  Returns:
    A tuple of adjusted `nnx.StateAxes` for each level of `vmap`.
  """
  if not state_axes_to_nest:
    return ()

  state_filters = {tuple(x.keys()) for x in state_axes_to_nest}
  if len(state_filters) != 1:
    raise ValueError(
        f'nesting state_in_axes requires same keys, got {state_filters}'
    )
  [state_filters] = list(state_filters)
  axes_by_filter = {
      f: tuple(s[f] for s in state_axes_to_nest) for f in state_filters
  }
  nested_axes_by_filter = {
      f: field_utils.nest_in_axes(*trees) for f, trees in axes_by_filter.items()
  }
  return tuple(
      nnx.StateAxes({f: nested_axes_by_filter[f][i] for f in state_filters})
      for i in range(len(state_axes_to_nest))
  )


class ModuleAndMethod(NamedTuple):
  module: nnx.Module
  method_name: str


def format_callbacks(callback_specs):
  """Formats callback_specs to standardized format."""
  if isinstance(callback_specs, ModuleAndMethod):
    return callback_specs
  if isinstance(callback_specs, nnx.Module):  # single callback.
    return ModuleAndMethod(callback_specs, '__call__')  # call default method.
  if isinstance(callback_specs, Iterable) and (len(callback_specs) == 2):
    return ModuleAndMethod(*callback_specs)
  raise TypeError(f'Unexpected {type(callback_specs)=}')


def with_callback(
    module,
    callback_specs: ModuleAndMethod,
    method_name: str = '__call__',
):
  """Returns module with `callback_specs.module` attached to `method_name`."""
  base_class = type(module)

  def __init__(self, wrapped_instance, callback_specs):  # pylint: disable=invalid-name
    self.wrapped_instance = wrapped_instance
    self.callback_specs = format_callbacks(callback_specs)

  def __getattr__(self, attr_name):  # pylint: disable=invalid-name
    """Delegate attribute access to the wrapped instance."""
    return getattr(self.wrapped_instance, attr_name)

  @functools.wraps(getattr(base_class, method_name))
  def wrapped_fn(self, *args, **kwargs):
    result = getattr(self.wrapped_instance, method_name)(*args, **kwargs)
    # The reason we use getattr here is because we need to access of method of
    # the callback module that is an attribute of this module. Otherwise nnx
    # would raise an error informing that we are trying to mutate an object that
    # is out of current scope. (This is exactly what would happen if we added
    # a reference to a callback_module.method as attribute of this class.)
    callback_fn = getattr(
        self.callback_specs.module, self.callback_specs.method_name
    )
    callback_fn(result, *args, **kwargs)
    return result

  attrs = {
      '__init__': __init__,
      '__getattr__': __getattr__,
      method_name: wrapped_fn,
  }
  if dataclasses.is_dataclass(base_class):
    for field in dataclasses.fields(base_class):
      attrs[field.name] = property(
          lambda self, field=field: getattr(self.wrapped_instance, field.name)
      )
  cls = type(base_class.__name__ + 'WithCallbacks', (base_class,), attrs)
  return cls(module, callback_specs)


def retrieve_subclass_modules(module, subclass):
  """Returns list of all unique `subclass` instances on `module`."""
  subclass_modules = []
  for _, x in module.iter_modules():
    if isinstance(x, subclass):
      subclass_modules.append(x)
  return subclass_modules
