from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

import numpy as np

from pychunkedgraph.backend import chunkedgraph_edits as cg_edits
from pychunkedgraph.backend import chunkedgraph_exceptions as cg_exceptions
from pychunkedgraph.backend.root_lock import RootLock
from pychunkedgraph.backend.utils import basetypes, column_keys, serializers

if TYPE_CHECKING:
    from pychunkedgraph.backend.chunkedgraph import ChunkedGraph
    from google.cloud import bigtable


class GraphEditOperation(ABC):
    __slots__ = ["cg", "user_id", "source_ids", "sink_ids", "source_coords", "sink_coords"]

    def __init__(
        self,
        cg: "ChunkedGraph",
        *,
        user_id: str,
        source_ids: Optional[Sequence[np.uint64]] = None,
        sink_ids: Optional[Sequence[np.uint64]] = None,
        source_coords: Optional[Sequence[Sequence[np.int]]] = None,
        sink_coords: Optional[Sequence[Sequence[np.int]]] = None,
    ) -> None:
        super().__init__()
        self.cg = cg
        self.user_id = user_id
        self.source_ids = source_ids
        self.sink_ids = sink_ids
        self.source_coords = source_coords
        self.sink_coords = sink_coords

        if self.source_ids is not None:
            self.source_ids = np.atleast_1d(source_ids).astype(basetypes.NODE_ID)
        if self.sink_ids is not None:
            self.sink_ids = np.atleast_1d(sink_ids).astype(basetypes.NODE_ID)
        if self.source_coords is not None:
            self.source_coords = np.atleast_2d(source_coords).astype(basetypes.COORDINATES)
        if self.sink_coords is not None:
            self.sink_coords = np.atleast_2d(sink_coords).astype(basetypes.COORDINATES)

        if self.sink_ids is not None and self.source_ids is not None:
            if np.any(np.in1d(self.sink_ids, self.source_ids)):
                raise cg_exceptions.PreconditionError(
                    f"One or more supervoxel exists as both, sink and source."
                )

            for source_id in self.source_ids:
                layer = self.cg.get_chunk_layer(source_id)
                if layer != 1:
                    raise cg_exceptions.PreconditionError(
                        f"Supervoxel expected, but {source_id} is a layer {layer} node."
                    )

            for sink_id in self.sink_ids:
                layer = self.cg.get_chunk_layer(sink_id)
                if layer != 1:
                    raise cg_exceptions.PreconditionError(
                        f"Supervoxel expected, but {sink_id} is a layer {layer} node."
                    )

    @staticmethod
    def from_log_record(
        cg: "ChunkedGraph",
        log_record: Dict[column_keys._Column, List["bigtable.row_data.Cell"]],
        *,
        multicut_as_split=True,
    ):
        user_id = log_record[column_keys.OperationLogs.UserID][0].value
        source_ids = log_record[column_keys.OperationLogs.SourceID][0].value
        sink_ids = log_record[column_keys.OperationLogs.SinkID][0].value
        source_coords = log_record[column_keys.OperationLogs.SourceCoordinate][0].value
        sink_coords = log_record[column_keys.OperationLogs.SinkCoordinate][0].value

        if column_keys.OperationLogs.AddedEdge in log_record:
            added_edges = log_record[column_keys.OperationLogs.AddedEdge][0].value
            affinities = log_record[column_keys.OperationLogs.Affinity][0].value
            return MergeOperation(
                cg,
                user_id=user_id,
                source_coords=source_coords,
                sink_coords=sink_coords,
                added_edges=added_edges,
                affinities=affinities,
            )

        if column_keys.OperationLogs.RemovedEdge in log_record:
            removed_edges = log_record[column_keys.OperationLogs.RemovedEdge][0].value
            if multicut_as_split or column_keys.OperationLogs.BoundingBoxOffset not in log_record:
                return SplitOperation(
                    cg,
                    user_id=user_id,
                    source_coords=source_coords,
                    sink_coords=sink_coords,
                    removed_edges=removed_edges,
                )
            else:
                bbox_offset = log_record[column_keys.OperationLogs.BoundingBoxOffset][0].value
                return MulticutOperation(
                    cg,
                    user_id=user_id,
                    source_coords=source_coords,
                    sink_coords=sink_coords,
                    bbox_offset=bbox_offset,
                    source_ids=source_ids,
                    sink_ids=sink_ids,
                )

        raise cg_exceptions.PreconditionError(
            f"Log record contains neither added nor removed edges."
        )

    @abstractmethod
    def apply(self) -> Tuple[np.ndarray, np.ndarray]:
        pass

    @abstractmethod
    def create_log_record(self, *, operation_id, timestamp, root_ids) -> "bigtable.row.Row":
        pass


class MergeOperation(GraphEditOperation):
    __slots__ = ["added_edges", "affinities"]

    def __init__(
        self,
        cg: "ChunkedGraph",
        *,
        user_id: str,
        added_edges: Sequence[Sequence[np.uint64]],
        source_coords: Optional[Sequence[Sequence[np.int]]] = None,
        sink_coords: Optional[Sequence[Sequence[np.int]]] = None,
        affinities: Optional[Sequence[np.float32]] = None,
    ) -> None:
        """Merge Operation: Connect *known* pairs of supervoxels by adding a (weighted) edge.

        :param cg: The ChunkedGraph object
        :type cg: ChunkedGraph
        :param user_id: User ID that will be assigned to this operation
        :type user_id: str
        :param added_edges: Supervoxel IDs of all added edges [[source, sink]]
        :type added_edges: Sequence[Sequence[np.uint64]]
        :param source_coords: world space coordinates in nm, corresponding to IDs in added_edges[:,0], defaults to None
        :type source_coords: Optional[Sequence[Sequence[np.int]]], optional
        :param sink_coords: world space coordinates in nm, corresponding to IDs in added_edges[:,1], defaults to None
        :type sink_coords: Optional[Sequence[Sequence[np.int]]], optional
        :param affinities: edge weights for newly added edges, entries corresponding to added_edges, defaults to None
        :type affinities: Optional[Sequence[np.float32]], optional
        """

        self.affinities = affinities
        self.added_edges = np.atleast_2d(added_edges).astype(basetypes.NODE_ID)
        source_ids, sink_ids = self.added_edges.transpose()

        if self.affinities is not None:
            self.affinities = np.atleast_1d(affinities).astype(basetypes.EDGE_AFFINITY)

        super().__init__(
            cg,
            user_id=user_id,
            source_ids=source_ids,
            sink_ids=sink_ids,
            source_coords=source_coords,
            sink_coords=sink_coords,
        )

    def apply(self) -> Tuple[np.ndarray, np.ndarray]:
        root_ids = np.unique(self.cg.get_roots(self.added_edges.ravel()))

        with RootLock(self.cg, root_ids) as root_lock:
            lock_operation_ids = np.array([root_lock.operation_id] * len(root_lock.locked_root_ids))
            timestamp = self.cg.read_consolidated_lock_timestamp(
                root_lock.locked_root_ids, lock_operation_ids
            )

            new_root_ids, new_lvl2_ids, rows = cg_edits.add_edges(
                self.cg,
                root_lock.operation_id,
                atomic_edges=self.added_edges,
                time_stamp=timestamp,
                affinities=self.affinities,
            )
            # FIXME: Remove once cg_edits.add_edges returns consistent type
            new_root_ids = np.array(new_root_ids, dtype=basetypes.NODE_ID)
            new_lvl2_ids = np.array(new_lvl2_ids, dtype=basetypes.NODE_ID)

            # Add a row to the log
            log_row = self.create_log_record(
                operation_id=root_lock.operation_id, timestamp=timestamp, root_ids=new_root_ids
            )

            # Put log row first!
            rows = [log_row] + rows

            # Execute write (makes sure that we are still owning the lock)
            self.cg.bulk_write(
                rows,
                root_lock.locked_root_ids,
                operation_id=root_lock.operation_id,
                slow_retry=False,
            )
            return new_root_ids, new_lvl2_ids

    def create_log_record(
        self, *, operation_id: np.uint64, timestamp: datetime, root_ids: Sequence[np.uint64]
    ) -> "bigtable.row.Row":
        """Create log record for this MergeOperation

        :param operation_id: Same operation ID used for the actual merge
        :type operation_id: np.uint64
        :param timestamp: Timestamp used for locking affected root objects and for the actual merge
        :type timestamp: datetime
        :param root_ids: New root IDs resulting from this MergeOperation
        :type root_ids: Sequence[np.uint64]
        :return: Row object containing all required information to recreate this MergeOperation
        :rtype: bigtable.row.Row
        """

        val_dict = {
            column_keys.OperationLogs.UserID: self.user_id,
            column_keys.OperationLogs.RootID: root_ids,
            column_keys.OperationLogs.AddedEdge: self.added_edges,
        }
        if self.source_coords is not None:
            val_dict[column_keys.OperationLogs.SourceCoordinate] = self.source_coords
        if self.sink_coords is not None:
            val_dict[column_keys.OperationLogs.SinkCoordinate] = self.sink_coords
        if self.affinities is not None:
            val_dict[column_keys.OperationLogs.Affinity] = self.affinities

        return self.cg.mutate_row(serializers.serialize_uint64(operation_id), val_dict, timestamp)


class SplitOperation(GraphEditOperation):
    __slots__ = ["removed_edges"]

    def __init__(
        self,
        cg: "ChunkedGraph",
        *,
        user_id: str,
        removed_edges: Sequence[Sequence[np.uint64]],
        source_coords: Optional[Sequence[Sequence[np.int]]] = None,
        sink_coords: Optional[Sequence[Sequence[np.int]]] = None,
    ) -> None:
        """Split Operation: Cut *known* pairs of supervoxel that are directly connected by an edge.

        :param cg: The ChunkedGraph object
        :type cg: ChunkedGraph
        :param user_id: User ID that will be assigned to this operation
        :type user_id: str
        :param removed_edges: Supervoxel IDs of all removed edges [[source, sink]]
        :type removed_edges: Sequence[Sequence[np.uint64]]
        :param source_coords: world space coordinates in nm, corresponding to IDs in
            removed_edges[:,0], defaults to None
        :type source_coords: Optional[Sequence[Sequence[np.int]]], optional
        :param sink_coords: world space coordinates in nm, corresponding to IDs in
            removed_edges[:,1], defaults to None
        :type sink_coords: Optional[Sequence[Sequence[np.int]]], optional
        """

        self.removed_edges = np.atleast_2d(removed_edges).astype(basetypes.NODE_ID)
        source_ids, sink_ids = self.removed_edges.transpose()

        super().__init__(
            cg,
            user_id=user_id,
            source_ids=source_ids,
            sink_ids=sink_ids,
            source_coords=source_coords,
            sink_coords=sink_coords,
        )

    def apply(self) -> Tuple[np.ndarray, np.ndarray]:
        source_and_sink_ids = [sid for l in (self.source_ids, self.sink_ids) for sid in l]
        root_ids = np.unique(self.cg.get_roots(source_and_sink_ids))

        if len(root_ids) > 1:
            raise cg_exceptions.PreconditionError(
                f"All supervoxel must belong to the same object. Already split?"
            )

        with RootLock(self.cg, root_ids) as root_lock:
            lock_operation_ids = np.array([root_lock.operation_id] * len(root_lock.locked_root_ids))
            timestamp = self.cg.read_consolidated_lock_timestamp(
                root_lock.locked_root_ids, lock_operation_ids
            )

            new_root_ids, new_lvl2_ids, rows = cg_edits.remove_edges(
                self.cg,
                root_lock.operation_id,
                atomic_edges=self.removed_edges,
                time_stamp=timestamp,
            )
            # FIXME: Remove once cg_edits.remove_edges returns consistent type
            new_root_ids = np.array(new_root_ids, dtype=basetypes.NODE_ID)
            new_lvl2_ids = np.array(new_lvl2_ids, dtype=basetypes.NODE_ID)

            # Add a row to the log
            log_row = self.create_log_record(
                operation_id=root_lock.operation_id, root_ids=new_root_ids, timestamp=timestamp
            )

            # Put log row first!
            rows = [log_row] + rows

            # Execute write (makes sure that we are still owning the lock)
            self.cg.bulk_write(
                rows,
                root_lock.locked_root_ids,
                operation_id=root_lock.operation_id,
                slow_retry=False,
            )
            return new_root_ids, new_lvl2_ids

    def create_log_record(
        self, *, operation_id: np.uint64, timestamp: datetime, root_ids: Sequence[np.uint64]
    ) -> "bigtable.row.Row":
        """Create log record for this SplitOperation

        :param operation_id: Same operation ID used for the actual split
        :type operation_id: np.uint64
        :param timestamp: Timestamp used for locking affected root objects and for the actual split
        :type timestamp: datetime
        :param root_ids: New root IDs resulting from this SplitOperation
        :type root_ids: Sequence[np.uint64]
        :return: Row object containing all required information to recreate this SplitOperation
        :rtype: bigtable.row.Row
        """

        val_dict = {
            column_keys.OperationLogs.UserID: self.user_id,
            column_keys.OperationLogs.RootID: root_ids,
            column_keys.OperationLogs.RemovedEdge: self.removed_edges,
        }
        if self.source_coords is not None:
            val_dict[column_keys.OperationLogs.SourceCoordinate] = self.source_coords
        if self.sink_coords is not None:
            val_dict[column_keys.OperationLogs.SinkCoordinate] = self.sink_coords

        return self.cg.mutate_row(serializers.serialize_uint64(operation_id), val_dict, timestamp)


class MulticutOperation(GraphEditOperation):
    __slots__ = ["removed_edges", "bbox_offset"]

    def __init__(
        self,
        cg: "ChunkedGraph",
        *,
        user_id: str,
        source_ids: Sequence[np.uint64],
        sink_ids: Sequence[np.uint64],
        source_coords: Sequence[Sequence[np.int]],
        sink_coords: Sequence[Sequence[np.int]],
        bbox_offset: Optional[Sequence[np.int]] = None,
    ) -> None:
        """Multicut Operation: Apply min-cut algorithm to identify suitable edges for removal
           in order to separate two groups of supervoxels.

        :param cg: The ChunkedGraph object
        :type cg: ChunkedGraph
        :param user_id: User ID that will be assigned to this operation
        :type user_id: str
        :param source_ids: Supervoxel IDs that should be separated from supervoxel IDs in sink_ids
        :type souce_ids: Sequence[np.uint64]
        :param sink_ids: Supervoxel IDs that should be separated from supervoxel IDs in source_ids
        :type sink_ids: Sequence[np.uint64]
        :param source_coords: world space coordinates in nm, corresponding to IDs in source_ids
        :type source_coords: Sequence[Sequence[np.int]]
        :param sink_coords: world space coordinates in nm, corresponding to IDs in sink_ids
        :type sink_coords: Sequence[Sequence[np.int]]
        :param bbox_offset: Padding for min-cut bounding box, applied to min/max coordinates
            retrieved from source_coords and sink_coords, defaults to None
        :type bbox_offset: Optional[Sequence[np.int]], optional
        """

        self.removed_edges = None  # Calculated from coordinates and IDs
        self.bbox_offset = bbox_offset

        if self.bbox_offset is not None:
            self.bbox_offset = np.atleast_1d(bbox_offset).astype(basetypes.COORDINATES)

        super().__init__(
            cg,
            user_id=user_id,
            source_ids=source_ids,
            sink_ids=sink_ids,
            source_coords=source_coords,
            sink_coords=sink_coords,
        )

    def apply(self) -> Tuple[np.ndarray, np.ndarray]:
        source_and_sink_ids = [sid for l in (self.source_ids, self.sink_ids) for sid in l]
        root_ids = np.unique(self.cg.get_roots(source_and_sink_ids))

        if len(root_ids) > 1:
            raise cg_exceptions.PreconditionError(
                f"All supervoxel must belong to the same object. Already split?"
            )

        with RootLock(self.cg, root_ids) as root_lock:
            lock_operation_ids = np.array([root_lock.operation_id] * len(root_lock.locked_root_ids))
            timestamp = self.cg.read_consolidated_lock_timestamp(
                root_lock.locked_root_ids, lock_operation_ids
            )

            self.removed_edges = self.cg._run_multicut(
                self.source_ids,
                self.sink_ids,
                self.source_coords,
                self.sink_coords,
                self.bbox_offset,
            )

            if self.removed_edges.size == 0:
                raise cg_exceptions.PostconditionError(
                    "Mincut could not find any edges to remove - weird!"
                )

            new_root_ids, new_lvl2_ids, rows = cg_edits.remove_edges(
                self.cg,
                root_lock.operation_id,
                atomic_edges=self.removed_edges,
                time_stamp=timestamp,
            )
            # FIXME: Remove once cg_edits.remove_edges returns consistent type
            new_root_ids = np.array(new_root_ids, dtype=basetypes.NODE_ID)
            new_lvl2_ids = np.array(new_lvl2_ids, dtype=basetypes.NODE_ID)

            # Add a row to the log
            log_row = self.create_log_record(
                operation_id=root_lock.operation_id, root_ids=new_root_ids, timestamp=timestamp
            )

            # Put log row first!
            rows = [log_row] + rows

            # Execute write (makes sure that we are still owning the lock)

            self.cg.bulk_write(
                rows,
                root_lock.locked_root_ids,
                operation_id=root_lock.operation_id,
                slow_retry=False,
            )
            return new_root_ids, new_lvl2_ids

    def create_log_record(
        self, *, operation_id: np.uint64, timestamp: datetime, root_ids: Sequence[np.uint64]
    ) -> "bigtable.row.Row":
        """Create log record for this MulticutOperation

        :param operation_id: Same operation ID used for the actual split
        :type operation_id: np.uint64
        :param timestamp: Timestamp used for locking affected root objects and for the actual split
        :type timestamp: datetime
        :param root_ids: New root IDs resulting from this MulticutOperation
        :type root_ids: Sequence[np.uint64]
        :return: Row object containing all required information to recreate this MulticutOperation
        :rtype: bigtable.row.Row
        """

        val_dict = {
            column_keys.OperationLogs.UserID: self.user_id,
            column_keys.OperationLogs.RootID: root_ids,
            column_keys.OperationLogs.SourceCoordinate: self.source_coords,
            column_keys.OperationLogs.SinkCoordinate: self.sink_coords,
            column_keys.OperationLogs.SourceID: self.source_ids,
            column_keys.OperationLogs.SinkID: self.sink_ids,
        }
        if self.bbox_offset is not None:
            val_dict[column_keys.OperationLogs.BoundingBoxOffset] = self.bbox_offset

        return self.cg.mutate_row(serializers.serialize_uint64(operation_id), val_dict, timestamp)
