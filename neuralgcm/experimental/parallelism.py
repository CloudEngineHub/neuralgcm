# Copyright 2025 Google LLC

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     https://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utilities for implementing SPMD model parallelism."""

import collections
import dataclasses
from flax import nnx
import jax
import jax.numpy as jnp
from neuralgcm.experimental import coordax as cx
from neuralgcm.experimental import pytree_utils
from neuralgcm.experimental import typing
import numpy as np

P = jax.sharding.PartitionSpec

# Type aliases for improved readability.
Schema = str
DimensionName = str
AxisPartition = str | tuple[str, ...] | None
ArrayPartition = tuple[AxisPartition, ...]
DimPartitions = dict[DimensionName, AxisPartition]

# Axis partitions specify how to partition a single dimension of an array, which
# can be either a single axis along the device mesh, specified as a `str`,
# a `tuple[str, ...]` if the axis is partitioned acorss multiple device axes
# or `None` if the dimension is not partitioned (i.e. replicated).

# Array partitions specify an AxisPartition per axis.
#
# For example, if we have a 3D array and we want to partition it across
# a device mesh `('x', 'y')`, along the first dimension, we would specify
#   `array_partition = (('x', 'y'), None, None)`.

# DimPartitions determines how each dimension of a field is split (partitioned)
# across multiple devices.
#
# You define this partitioning by associating each dimension with a device mesh.
# For example, if you have a device mesh defined as ('x', 'y', 'z') and you set
#   `dim_partitions = {'level': ('x', 'z'), 'lat': 'y'}`
# it means: (1) The 'level' dimension will be divided across the 'x' and 'z'
# devices of the mesh. (2) The 'lat' dimension will be divided across the 'y'
# device of the mesh. (3) Any dimensions not mentioned will be fully replicated
# on all devices.


def partition_spec_for_field(
    field: cx.Field, dim_partitions: DimPartitions
) -> P:
  """Returns partition spec for `field`."""
  return P(*[dim_partitions.get(d, None) for d in field.dims])


# TODO(dkochkov): drop array_partitions specification when partitions can be
# specified using field_partitions.


@dataclasses.dataclass
class Mesh(nnx.Module):
  """Holds the device mesh and array/field partitioning strategies.

  Attributes:
    spmd_mesh: Jax sharding mesh object.
    array_partitions: Schemas for partitioning arrays. An array partition
      specifies how to partition a an array of a specific rank. For example,
      to partition a 3d array across a device mesh `('x', 'y')`, along the
      first dimension, we would use a `(('x', 'y'), None, None)` schema.
    field_partitions: Schemas for partitioning cx.Field. Each schema provides
      partitioning rules for array axes specified by the dimension names. For
      example, schema `{'vertical': {'level': ('z', 'x', 'y'), 'layer': 'z'}}`
      specifies that all `level` dimensions of a field will be partitioned
      across the `z`, `x`, and `y` axes of the device mesh and all `layer`
      dimensions will be partitioned only across the `z` axis.
  """

  spmd_mesh: jax.sharding.Mesh | None = None
  array_partitions: dict[Schema, ArrayPartition] = dataclasses.field(
      default_factory=dict
  )
  field_partitions: dict[Schema, DimPartitions] = dataclasses.field(
      default_factory=dict
  )

  def __post_init__(self):
    self._validate_partitions()

  def _validate_partitions(self):
    """Validates that partitioning options are compatible with the mesh."""
    if self.spmd_mesh is not None:
      for k, v in self.array_partitions.items():
        partition_axes = jax.tree.leaves(v)
        if not set(partition_axes).issubset(set(self.axis_names)):
          raise ValueError(f'Spec {k, v} use axes not in {self.axis_names}')
        if np.unique(partition_axes).size != len(partition_axes):
          raise ValueError(f'Encountered duplicate in spec {k, v}')

      for _, dim_partitions in self.field_partitions.items():
        for k, v in dim_partitions.items():
          partition_axes = jax.tree.leaves(v)
          if not set(partition_axes).issubset(set(self.axis_names)):
            raise ValueError(f'Spec {k, v} use axes not in {self.axis_names}')
          if np.unique(partition_axes).size != len(partition_axes):
            raise ValueError(f'Encountered duplicate in spec {k, v}')

  @property
  def shape(self) -> collections.OrderedDict[str, int]:
    """Shape of the mesh."""
    return self.spmd_mesh.shape if self.spmd_mesh else collections.OrderedDict()

  @property
  def axis_names(self) -> tuple[str, ...]:
    """Names of the mesh axes."""
    return self.spmd_mesh.axis_names if self.spmd_mesh else ()

  def with_sharding_constraint(
      self, inputs: typing.PyTreeState, schema: str | tuple[str, ...]
  ) -> typing.PyTreeState:
    """Applies `schema` sharding constraint to `inputs`."""
    if isinstance(schema, tuple):
      if not schema:  # case when we processed all constraints
        return inputs
      constrained = self.with_sharding_constraint(inputs, schema[1:])
      return self.with_sharding_constraint(constrained, schema[0])

    is_field = lambda x: isinstance(x, cx.Field)
    all_leaves, tree_def = jax.tree.flatten(inputs, is_leaf=is_field)
    array_leaves = [x for x in all_leaves if not is_field(x)]
    if np.unique([x.ndim for x in array_leaves]).size > 1:
      raise ValueError(
          'All arrays in the pytree must have the same rank. Got:'
          f' {[x.ndim for x in array_leaves]=}'
      )
    if self.spmd_mesh is None:
      return inputs
    leaves = []
    for x in all_leaves:
      if is_field(x):
        leaves.append(self._with_sharding_constraint_field(x, schema))
      else:
        leaves.append(self._with_sharding_constraint_array(x, schema))
    return jax.tree.unflatten(tree_def, leaves)

  def _with_sharding_constraint_array(
      self, array: jax.Array, schema: str
  ) -> jax.Array:
    """Applies sharding constraint to `array`.

    Args:
      array: array to apply sharding constraint to.
      schema: key in `self.array_partitions` indicating sharding schema.

    Returns:
      `inputs` with sharding constraint(s) applied.
    """
    p_specs = P(*self.array_partitions[schema])
    sharding = jax.sharding.NamedSharding(self.spmd_mesh, p_specs)
    return jax.lax.with_sharding_constraint(array, sharding)

  def _with_sharding_constraint_field(
      self, field: cx.Field, schema: str
  ) -> cx.Field:
    """Applies sharding constraint to `field`.

    Args:
      field: field to apply sharding constraint to.
      schema: key in `self.field_partitions` indicating sharding schema.

    Returns:
      Field with sharding constraint applied.
    """
    dim_partitions = self.field_partitions[schema]
    p_specs = partition_spec_for_field(field, dim_partitions)
    sharding = jax.sharding.NamedSharding(self.spmd_mesh, p_specs)
    return jax.lax.with_sharding_constraint(field, sharding)


# TODO(dkochkov): Remove these temporary functions once we can rely on imposing
# sharding constraints on coordax.Field leaves.
def with_dycore_sharding(
    mesh: Mesh | None,
    inputs: typing.PyTreeState,
) -> typing.PyTreeState:
  """Applies even sharding variants to inputs depending on input shape."""
  def f(y: jax.Array) -> jax.Array:
    assert isinstance(mesh, Mesh)  # make pytype happy.
    if y.ndim == 1 and y.dtype == jnp.uint32:
      return y
    elif y.ndim == 3 and y.shape[0] != 1:
      return mesh.with_sharding_constraint(y, 'dycore_3d')
    elif y.ndim == 3 and y.shape[0] == 1:
      return mesh.with_sharding_constraint(y, 'dycore_3d_surface')
    elif y.ndim == 2:
      return mesh.with_sharding_constraint(y, 'dycore_2d')
    else:
      raise ValueError(f'Unsupported array shape: {y.shape}')

  if mesh is None:
    return inputs
  return pytree_utils.tree_map_over_nonscalars(f, inputs)


def with_physics_sharding(
    mesh: Mesh | None,
    inputs: typing.PyTreeState,
) -> typing.PyTreeState:
  """Applies horizontal sharding variants depending on input shape."""
  def f(y: jax.Array) -> jax.Array:
    assert isinstance(mesh, Mesh)  # make pytype happy.
    if y.ndim == 1 and y.dtype == jnp.uint32:
      return y
    elif y.ndim == 3:
      return mesh.with_sharding_constraint(y, 'physics_3d')
    elif y.ndim == 2:
      return mesh.with_sharding_constraint(y, 'physics_2d')
    else:
      raise ValueError(f'Unsupported array shape: {y.shape}')

  if mesh is None:
    return inputs
  return pytree_utils.tree_map_over_nonscalars(f, inputs)


def with_dycore_to_physics_sharding(
    mesh: Mesh | None,
    inputs: typing.PyTreeState,
) -> typing.PyTreeState:
  """Applies dycore sharding followed by physics sharding constraints."""
  return with_physics_sharding(mesh, with_dycore_sharding(mesh, inputs))


def with_physics_to_dycore_sharding(
    mesh: Mesh | None,
    inputs: typing.PyTreeState,
) -> typing.PyTreeState:
  """Applies physics sharding followed by dycore sharding constraints."""
  return with_dycore_sharding(mesh, with_physics_sharding(mesh, inputs))
