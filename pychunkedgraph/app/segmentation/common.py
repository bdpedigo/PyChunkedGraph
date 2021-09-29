import collections
import json
import threading
import time
import traceback
import gzip
import os
import requests
from io import BytesIO as IO
from datetime import datetime
from functools import reduce

import numpy as np
from pytz import UTC
import pandas as pd

from cloudvolume import compression

from flask import current_app, g, jsonify, make_response, request
from pychunkedgraph import __version__
from pychunkedgraph.app import app_utils
from pychunkedgraph.graph import (
    attributes,
    cutting,
    exceptions as cg_exceptions,
    edges as cg_edges,
)
from pychunkedgraph.graph import segmenthistory
from pychunkedgraph.graph.analysis import pathing
from pychunkedgraph.graph.attributes import OperationLogs
from pychunkedgraph.meshing import mesh_analysis
from pychunkedgraph.graph.misc import get_contact_sites
from middle_auth_client import get_usernames


__api_versions__ = [0, 1]
__segmentation_url_prefix__ = os.environ.get("SEGMENTATION_URL_PREFIX", "segmentation")


def index():
    return f"PyChunkedGraph Segmentation v{__version__}"


def home():
    resp = make_response()
    resp.headers["Access-Control-Allow-Origin"] = "*"
    acah = "Origin, X-Requested-With, Content-Type, Accept"
    resp.headers["Access-Control-Allow-Headers"] = acah
    resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    resp.headers["Connection"] = "keep-alive"
    return resp


# -------------------------------
# ------ Measurements and Logging
# -------------------------------


def before_request():
    current_app.request_start_time = time.time()
    current_app.request_start_date = datetime.utcnow()
    current_app.user_id = None
    current_app.table_id = None
    current_app.request_type = None

    content_encoding = request.headers.get("Content-Encoding", "")

    if "gzip" in content_encoding.lower():
        request.data = compression.decompress(request.data, "gzip")


def after_request(response):
    dt = (time.time() - current_app.request_start_time) * 1000

    current_app.logger.debug("Response time: %.3fms" % dt)

    try:
        if current_app.user_id is None:
            user_id = ""
        else:
            user_id = current_app.user_id

        if current_app.table_id is not None:
            log_db = app_utils.get_log_db(current_app.table_id)
            log_db.add_success_log(
                user_id=user_id,
                user_ip="",
                request_time=current_app.request_start_date,
                response_time=dt,
                url=request.url,
                request_data=request.data,
                request_type=current_app.request_type,
            )
    except Exception as e:
        current_app.logger.debug(
            f"{current_app.user_id}: LogDB entry not" f" successful: {e}"
        )

    accept_encoding = request.headers.get("Accept-Encoding", "")

    if "gzip" not in accept_encoding.lower():
        return response

    response.direct_passthrough = False

    if (
        response.status_code < 200
        or response.status_code >= 300
        or "Content-Encoding" in response.headers
    ):
        return response

    response.data = compression.gzip_compress(response.data)

    response.headers["Content-Encoding"] = "gzip"
    response.headers["Vary"] = "Accept-Encoding"
    response.headers["Content-Length"] = len(response.data)

    return response


def unhandled_exception(e):
    status_code = 500
    response_time = (time.time() - current_app.request_start_time) * 1000
    user_ip = str(request.remote_addr)
    tb = traceback.format_exception(etype=type(e), value=e, tb=e.__traceback__)

    current_app.logger.error(
        {
            "message": str(e),
            "user_id": user_ip,
            "user_ip": user_ip,
            "request_time": current_app.request_start_date,
            "request_url": request.url,
            "request_data": request.data,
            "response_time": response_time,
            "response_code": status_code,
            "traceback": tb,
        }
    )

    resp = {
        "timestamp": current_app.request_start_date,
        "duration": response_time,
        "code": status_code,
        "message": str(e),
        "traceback": tb,
    }

    return jsonify(resp), status_code


def api_exception(e):
    response_time = (time.time() - current_app.request_start_time) * 1000
    user_ip = str(request.remote_addr)
    tb = traceback.format_exception(etype=type(e), value=e, tb=e.__traceback__)

    current_app.logger.error(
        {
            "message": str(e),
            "user_id": user_ip,
            "user_ip": user_ip,
            "request_time": current_app.request_start_date,
            "request_url": request.url,
            "request_data": request.data,
            "response_time": response_time,
            "response_code": e.status_code.value,
            "traceback": tb,
        }
    )

    resp = {
        "timestamp": current_app.request_start_date,
        "duration": response_time,
        "code": e.status_code.value,
        "message": str(e),
    }

    return jsonify(resp), e.status_code.value


def _parse_timestamp(arg_name, default_timestamp=0, return_datetime=False):
    """Convert seconds since epoch to UTC datetime."""
    timestamp = request.args.get(arg_name, default_timestamp)
    if timestamp is None:
        raise (cg_exceptions.BadRequest(f"Timestamp parameter {arg_name} is mandatory"))
    try:
        timestamp = float(timestamp)
        if return_datetime:
            return datetime.fromtimestamp(timestamp, UTC)
        else:
            return timestamp
    except (TypeError, ValueError):
        raise (
            cg_exceptions.BadRequest(
                f"Timestamp parameter {arg_name} is not a valid unix timestamp"
            )
        )


# -------------------
# ------ Applications
# -------------------


def sleep_me(sleep):
    current_app.request_type = "sleep"

    time.sleep(sleep)
    return "zzz... {} ... awake".format(sleep)


def handle_info(table_id):
    cg = app_utils.get_cg(table_id)
    dataset_info = cg.meta.dataset_info
    app_info = {"app": {"supported_api_versions": list(__api_versions__)}}
    combined_info = {**dataset_info, **app_info}
    combined_info["sharded_mesh"] = True
    combined_info["verify_mesh"] = cg.meta.custom_data.get("mesh", {}).get(
        "verify", False
    )
    combined_info["mesh"] = cg.meta.custom_data.get("mesh", {}).get(
        "dir", "graphene_meshes"
    )
    return jsonify(combined_info)


def handle_api_versions():
    return jsonify(__api_versions__)


### GET ROOT -------------------------------------------------------------------


def handle_root(table_id, atomic_id):
    current_app.table_id = table_id

    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    # Convert seconds since epoch to UTC datetime
    timestamp = _parse_timestamp("timestamp", time.time(), return_datetime=True)

    stop_layer = request.args.get("stop_layer", None)
    if stop_layer is not None:
        try:
            stop_layer = int(stop_layer)
        except (TypeError, ValueError) as e:
            raise (cg_exceptions.BadRequest(f"stop_layer is not an integer {e}"))

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)
    root_id = cg.get_root(
        np.uint64(atomic_id), stop_layer=stop_layer, time_stamp=timestamp
    )

    # Return root ID
    return root_id


### GET ROOTS -------------------------------------------------------------------


def handle_roots(table_id, is_binary=False):
    current_app.request_type = "roots"
    current_app.table_id = table_id

    if is_binary:
        node_ids = np.frombuffer(request.data, np.uint64)
    else:
        node_ids = np.array(json.loads(request.data)["node_ids"], dtype=np.uint64)
    # Convert seconds since epoch to UTC datetime
    timestamp = _parse_timestamp("timestamp", time.time(), return_datetime=True)

    cg = app_utils.get_cg(table_id)
    stop_layer = int(request.args.get("stop_layer", cg.meta.layer_count))
    is_root_layer = stop_layer == cg.meta.layer_count
    assert_roots = bool(request.args.get("assert_roots", False))
    root_ids = cg.get_roots(
        node_ids,
        stop_layer=stop_layer,
        time_stamp=timestamp,
        assert_roots=assert_roots and is_root_layer,
    )

    return root_ids


### RANGE READ -------------------------------------------------------------------


def handle_l2_chunk_children(table_id, chunk_id, as_array):
    current_app.request_type = "l2_chunk_children"
    current_app.table_id = table_id

    # Convert seconds since epoch to UTC datetime
    timestamp = _parse_timestamp("timestamp", time.time(), return_datetime=True)

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)

    chunk_layer = cg.get_chunk_layer(chunk_id)
    if chunk_layer != 2:
        raise (
            cg_exceptions.PreconditionError(
                f"This function only accepts level 2 chunks, the chunk requested is a level {chunk_layer} chunk"
            )
        )

    rr_chunk = cg.range_read_chunk(
        chunk_id=np.uint64(chunk_id),
        properties=attributes.Hierarchy.Child,
        time_stamp=timestamp,
    )

    if as_array:
        l2_chunk_array = []

        for l2 in rr_chunk:
            svs = rr_chunk[l2][0].value
            for sv in svs:
                l2_chunk_array.extend([l2, sv])

        return np.array(l2_chunk_array)
    else:
        # store in dict of keys to arrays to remove reliance on bigtable
        l2_chunk_dict = {}
        for k in rr_chunk:
            l2_chunk_dict[k] = rr_chunk[k][0].value

        return l2_chunk_dict


def str2bool(v):
    return v.lower() in ("yes", "true", "t", "1")


def publish_edit(table_id, new_lvl2_ids, is_priority=True):
    from messagingclient import MessagingClient

    payload = np.array(new_lvl2_ids, dtype=np.uint64).tobytes()
    attributes = {
        "table_id": table_id,
    }

    exchange = os.getenv("PYCHUNKEDGRAPH_EDITS_EXCHANGE", "pychunkedgraph")
    c = MessagingClient()
    c.publish(exchange, payload, attributes)


### MERGE ----------------------------------------------------------------------


def handle_merge(table_id, allow_same_segment_merge=False):
    current_app.table_id = table_id

    nodes = json.loads(request.data)
    is_priority = request.args.get("priority", True, type=str2bool)
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    current_app.logger.debug(nodes)
    assert len(nodes) == 2

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id, skip_cache=True)
    node_ids = []
    coords = []
    for node in nodes:
        node_ids.append(node[0])
        coords.append(np.array(node[1:]) / cg.segmentation_resolution)

    atomic_edge = app_utils.handle_supervoxel_id_lookup(cg, coords, node_ids)
    # Protection from long range mergers
    chunk_coord_delta = cg.get_chunk_coordinates(
        atomic_edge[0]
    ) - cg.get_chunk_coordinates(atomic_edge[1])

    if np.any(np.abs(chunk_coord_delta) > 3):
        raise cg_exceptions.BadRequest(
            "Chebyshev distance between merge points exceeded allowed maximum "
            "(3 chunks)."
        )

    try:
        ret = cg.add_edges(
            user_id=user_id,
            atomic_edges=np.array(atomic_edge, dtype=np.uint64),
            source_coords=coords[:1],
            sink_coords=coords[1:],
            allow_same_segment_merge=allow_same_segment_merge,
        )

    except cg_exceptions.LockingError as e:
        raise cg_exceptions.InternalServerError(e)
    except cg_exceptions.PreconditionError as e:
        raise cg_exceptions.BadRequest(str(e))

    if ret.new_root_ids is None:
        raise cg_exceptions.InternalServerError(
            "Could not merge selected " "supervoxel."
        )

    current_app.logger.debug(("lvl2_nodes:", ret.new_lvl2_ids))

    if len(ret.new_lvl2_ids) > 0:
        publish_edit(table_id, ret.new_lvl2_ids, is_priority=is_priority)

    return ret


### SPLIT ----------------------------------------------------------------------


def handle_split(table_id):
    current_app.table_id = table_id

    data = json.loads(request.data)
    is_priority = request.args.get("priority", True, type=str2bool)
    mincut = request.args.get("mincut", True, type=str2bool)
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    current_app.logger.debug(data)

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id, skip_cache=True)
    node_idents = []
    node_ident_map = {
        "sources": 0,
        "sinks": 1,
    }
    coords = []
    node_ids = []

    for k in ["sources", "sinks"]:
        for node in data[k]:
            node_ids.append(node[0])
            coords.append(np.array(node[1:]) / cg.segmentation_resolution)
            node_idents.append(node_ident_map[k])

    node_ids = np.array(node_ids, dtype=np.uint64)
    coords = np.array(coords)
    node_idents = np.array(node_idents)
    sv_ids = app_utils.handle_supervoxel_id_lookup(cg, coords, node_ids)
    current_app.logger.debug(
        {"node_id": node_ids, "sv_id": sv_ids, "node_ident": node_idents}
    )

    try:
        ret = cg.remove_edges(
            user_id=user_id,
            source_ids=sv_ids[node_idents == 0],
            sink_ids=sv_ids[node_idents == 1],
            source_coords=coords[node_idents == 0],
            sink_coords=coords[node_idents == 1],
            mincut=mincut,
        )
    except cg_exceptions.LockingError as e:
        raise cg_exceptions.InternalServerError(e)
    except cg_exceptions.PreconditionError as e:
        raise cg_exceptions.BadRequest(str(e))

    if ret.new_root_ids is None:
        raise cg_exceptions.InternalServerError(
            "Could not split selected segment groups."
        )

    current_app.logger.debug(("after split:", ret.new_root_ids))
    current_app.logger.debug(("lvl2_nodes:", ret.new_lvl2_ids))

    if len(ret.new_lvl2_ids) > 0:
        publish_edit(table_id, ret.new_lvl2_ids, is_priority=is_priority)

    return ret


### UNDO ----------------------------------------------------------------------


def handle_undo(table_id):
    current_app.table_id = table_id

    data = json.loads(request.data)
    is_priority = request.args.get("priority", True, type=str2bool)
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    current_app.logger.debug(data)

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)
    operation_id = np.uint64(data["operation_id"])

    try:
        ret = cg.undo_operation(user_id=user_id, operation_id=operation_id)
    except cg_exceptions.LockingError as e:
        raise cg_exceptions.InternalServerError(e)
    except (cg_exceptions.PreconditionError, cg_exceptions.PostconditionError) as e:
        raise cg_exceptions.BadRequest(str(e))

    current_app.logger.debug(("after undo:", ret.new_root_ids))
    current_app.logger.debug(("lvl2_nodes:", ret.new_lvl2_ids))

    if ret.new_lvl2_ids.size > 0:
        publish_edit(table_id, ret.new_lvl2_ids, is_priority=is_priority)

    return ret


### REDO ----------------------------------------------------------------------


def handle_redo(table_id):
    current_app.table_id = table_id

    data = json.loads(request.data)
    is_priority = request.args.get("priority", True, type=str2bool)
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    current_app.logger.debug(data)

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)
    operation_id = np.uint64(data["operation_id"])

    try:
        ret = cg.redo_operation(user_id=user_id, operation_id=operation_id)
    except cg_exceptions.LockingError as e:
        raise cg_exceptions.InternalServerError(e)
    except (cg_exceptions.PreconditionError, cg_exceptions.PostconditionError) as e:
        raise cg_exceptions.BadRequest(str(e))

    current_app.logger.debug(("after redo:", ret.new_root_ids))
    current_app.logger.debug(("lvl2_nodes:", ret.new_lvl2_ids))

    if ret.new_lvl2_ids.size > 0:
        publish_edit(table_id, ret.new_lvl2_ids, is_priority=is_priority)

    return ret


### ROLLBACK USER --------------------------------------------------------------


def handle_rollback(table_id):
    current_app.table_id = table_id

    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id
    target_user_id = request.args["user_id"]

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)
    user_operations = all_user_operations(table_id)
    operation_ids = user_operations["operation_id"]
    timestamps = user_operations["timestamp"]
    operations = list(zip(operation_ids, timestamps))
    operations.sort(key=lambda op: op[1], reverse=True)

    for operation in operations:
        operation_id = operation[0]
        try:
            ret = cg.undo_operation(user_id=target_user_id, operation_id=operation_id)
        except cg_exceptions.LockingError as e:
            raise cg_exceptions.InternalServerError(
                "Could not acquire root lock for undo operation."
            )
        except (cg_exceptions.PreconditionError, cg_exceptions.PostconditionError) as e:
            raise cg_exceptions.BadRequest(str(e))

        if ret.new_lvl2_ids.size > 0:
            publish_edit(table_id, ret.new_lvl2_ids, is_priority=is_priority)

    return user_operations


### USER OPERATIONS -------------------------------------------------------------


def all_user_operations(table_id, include_undone=False):
    # Gets all operations by the user.
    # If include_undone is false, it filters to operations that are not undone.
    # If the operation has been undone by anyone, it won't be returned here,
    # unless it has been redone by anyone (and hasn't been undone again, etc.).
    # The original user is considered to have "ownership" of the original edit,
    # and that does not change even if someone else undoes/redoes that edit later.
    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id
    target_user_id = request.args["user_id"]

    start_time = _parse_timestamp("start_time", 0, return_datetime=True)

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)

    log_rows = cg.client.read_log_entries(start_time=start_time)

    valid_entry_ids = []
    timestamp_list = []
    undone_ids = np.array([])

    entry_ids = np.sort(list(log_rows.keys()))
    for entry_id in entry_ids:
        entry = log_rows[entry_id]
        user_id = entry[OperationLogs.UserID]

        if user_id == target_user_id:
            valid_entry_ids.append(entry_id)
            timestamp = entry["timestamp"]
            timestamp_list.append(timestamp)

        should_check = (
            not OperationLogs.Status in entry
            or entry[OperationLogs.Status] == OperationLogs.StatusCodes.SUCCESS.value
        )

        if should_check:
            # if it is an undo of another operation, mark it as undone
            if OperationLogs.UndoOperationID in entry:
                undone_id = entry[OperationLogs.UndoOperationID]
                undone_ids = np.append(undone_ids, undone_id)

            # if it is a redo of another operation, unmark it as undone
            if OperationLogs.RedoOperationID in entry:
                redone_id = entry[OperationLogs.RedoOperationID]
                undone_ids = np.delete(undone_ids, np.argwhere(undone_ids == redone_id))

    if include_undone:
        return {"operation_id": valid_entry_ids, "timestamp": timestamp_list}

    filtered_entry_ids = []
    filtered_timestamp_list = []
    for i in range(len(valid_entry_ids)):
        entry_id = valid_entry_ids[i]
        entry = log_rows[entry_id]

        if (
            OperationLogs.UndoOperationID in entry
            or OperationLogs.RedoOperationID in entry
        ):
            continue

        undone = entry_id in undone_ids
        if not undone:
            filtered_entry_ids.append(entry_id)
            timestamp = entry["timestamp"]
            filtered_timestamp_list.append(timestamp)

    return {"operation_id": filtered_entry_ids, "timestamp": filtered_timestamp_list}


### CHILDREN -------------------------------------------------------------------


def handle_children(table_id, parent_id):
    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    cg = app_utils.get_cg(table_id)

    parent_id = np.uint64(parent_id)
    layer = cg.get_chunk_layer(parent_id)

    if layer > 1:
        children = cg.get_children(parent_id)
    else:
        children = np.array([])

    return children


### LEAVES ---------------------------------------------------------------------


def handle_leaves(table_id, root_id):
    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    stop_layer = int(request.args.get("stop_layer", 1))
    bounding_box = None
    if "bounds" in request.args:
        bounds = request.args["bounds"]
        bounding_box = np.array([b.split("-") for b in bounds.split("_")], dtype=int).T

    cg = app_utils.get_cg(table_id)
    if stop_layer > 1:
        from pychunkedgraph.graph.types import empty_1d

        subgraph = cg.get_subgraph_nodes(
            int(root_id),
            bbox=bounding_box,
            bbox_is_coordinate=True,
            return_layers=[stop_layer],
            return_flattened=True,
        )

        return subgraph
    return cg.get_subgraph_leaves(
        int(root_id),
        bbox=bounding_box,
        bbox_is_coordinate=True,
    )


### LEAVES OF MANY ROOTS ---------------------------------------------------------------------


def handle_leaves_many(table_id):
    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    if "bounds" in request.args:
        bounds = request.args["bounds"]
        bounding_box = np.array([b.split("-") for b in bounds.split("_")], dtype=int).T
    else:
        bounding_box = None

    node_ids = np.array(json.loads(request.data)["node_ids"], dtype=np.uint64)
    stop_layer = int(request.args.get("stop_layer", 1))

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)

    node_to_leaves_mapping = cg.get_subgraph_nodes(
        node_ids,
        bbox=bounding_box,
        bbox_is_coordinate=True,
        return_layers=[stop_layer],
        serializable=True,
        return_flattened=True,
    )

    return node_to_leaves_mapping


### LEAVES FROM LEAVES ---------------------------------------------------------


def handle_leaves_from_leave(table_id, atomic_id):
    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    if "bounds" in request.args:
        bounds = request.args["bounds"]
        bounding_box = np.array([b.split("-") for b in bounds.split("_")], dtype=int).T
    else:
        bounding_box = None

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)
    root_id = cg.get_root(int(atomic_id))

    atomic_ids = cg.get_subgraph(
        root_id, bbox=bounding_box, bbox_is_coordinate=True, nodes_only=True
    )

    return np.concatenate([np.array([root_id]), atomic_ids])


### SUBGRAPH -------------------------------------------------------------------


def handle_subgraph(table_id, root_id):
    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    if "bounds" in request.args:
        bounds = request.args["bounds"]
        bounding_box = np.array([b.split("-") for b in bounds.split("_")], dtype=int).T
    else:
        bounding_box = None

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)
    l2id_agglomeration_d, edges = cg.get_subgraph(
        int(root_id),
        bbox=bounding_box,
        bbox_is_coordinate=True,
    )
    edges = reduce(lambda x, y: x + y, edges, cg_edges.Edges([], []))
    supervoxels = np.concatenate(
        [agg.supervoxels for agg in l2id_agglomeration_d.values()]
    )
    mask0 = np.in1d(edges.node_ids1, supervoxels)
    mask1 = np.in1d(edges.node_ids2, supervoxels)
    edges = edges[mask0 & mask1]

    return edges


### CHANGE LOG -----------------------------------------------------------------


def change_log(table_id, root_id=None, filtered=False):
    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id
    time_stamp_past = _parse_timestamp("timestamp", 0, return_datetime=True)

    cg = app_utils.get_cg(table_id)
    if not root_id:
        return segmenthistory.get_all_log_entries(cg)
    history = segmenthistory.SegmentHistory(
        cg, [int(root_id)], timestamp_past=time_stamp_past
    )
    return history.change_log_summary(filtered=filtered)


def tabular_change_log_recent(table_id):
    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    start_time = _parse_timestamp("timestamp", 0, return_datetime=True)

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)

    log_rows = cg.client.read_log_entries(start_time=start_time)

    timestamp_list = []
    user_list = []

    operation_ids = np.sort(list(log_rows.keys()))
    for operation_id in operation_ids:
        operation = log_rows[operation_id]

        timestamp = operation["timestamp"]
        timestamp_list.append(timestamp)

        user_id = operation[attributes.OperationLogs.UserID]
        user_list.append(user_id)

    return pd.DataFrame.from_dict(
        {
            "operation_id": operation_ids,
            "timestamp": timestamp_list,
            "user_id": user_list,
        }
    )


def tabular_change_logs(table_id, root_ids, filtered=False):
    current_app.request_type = "tabular_changelog_many"

    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)
    history = segmenthistory.SegmentHistory(
        cg,
        root_ids,
    )
    if filtered:
        tab = history.tabular_changelogs_filtered
    else:
        tab = history.tabular_changelogs
    all_user_ids = []
    for tab_k in tab.keys():
        all_user_ids.extend(np.array(tab[tab_k]["user_id"]).reshape(-1))

    all_user_ids = np.unique(all_user_ids)
    user_dict = app_utils.get_username_dict(
        all_user_ids, current_app.config["AUTH_TOKEN"]
    )
    for tab_k in tab.keys():
        user_names = [
            user_dict.get(int(id_), "unknown")
            for id_ in np.array(tab[tab_k]["user_id"])
        ]
        tab[tab_k]["user_name"] = user_names
    return tab


def merge_log(table_id, root_id):
    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)
    hist = segmenthistory.SegmentHistory(cg, int(root_id))
    return hist.merge_log(correct_for_wrong_coord_type=False)


def handle_lineage_graph(table_id, root_id=None):
    from networkx import node_link_data
    from pychunkedgraph.graph.lineage import lineage_graph

    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    timestamp_past = _parse_timestamp("timestamp_past", 0, return_datetime=True)
    timestamp_future = _parse_timestamp(
        "timestamp_future", time.time(), return_datetime=True
    )

    cg = app_utils.get_cg(table_id)
    if root_id is None:
        root_ids = np.array(json.loads(request.data)["root_ids"], dtype=np.uint64)
        graph = lineage_graph(cg, root_ids, timestamp_past, timestamp_future)
        return node_link_data(graph)
    history_ids = segmenthistory.SegmentHistory(
        cg, int(root_id), timestamp_past, timestamp_future
    )
    return node_link_data(history_ids.lineage_graph)


def handle_past_id_mapping(table_id):
    root_ids = np.array(json.loads(request.data)["root_ids"], dtype=np.uint64)
    timestamp_past = _parse_timestamp(
        "timestamp_past", default_timestamp=0, return_datetime=True
    )
    timestamp_future = _parse_timestamp(
        "timestamp_future", default_timestamp=time.time(), return_datetime=True
    )

    cg = app_utils.get_cg(table_id)
    hist = segmenthistory.SegmentHistory(
        cg, root_ids, timestamp_past=timestamp_past, timestamp_future=timestamp_future
    )
    past_id_mapping, future_id_mapping = hist.past_future_id_mapping()
    return {
        "past_id_map": {str(k): past_id_mapping[k] for k in past_id_mapping.keys()},
        "future_id_map": {
            str(k): future_id_mapping[k] for k in future_id_mapping.keys()
        },
    }


def last_edit(table_id, root_id):
    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    cg = app_utils.get_cg(table_id)
    hist = segmenthistory.SegmentHistory(cg, int(root_id))
    return hist.last_edit_timestamp(int(root_id))


def oldest_timestamp(table_id):
    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id
    cg = app_utils.get_cg(table_id)
    return cg.get_earliest_timestamp()


### CONTACT SITES --------------------------------------------------------------


def handle_contact_sites(table_id, root_id):
    partners = request.args.get("partners", True, type=app_utils.toboolean)

    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    timestamp = _parse_timestamp("timestamp", time.time(), return_datetime=True)

    if "bounds" in request.args:
        bounds = request.args["bounds"]
        bounding_box = np.array([b.split("-") for b in bounds.split("_")], dtype=int).T
    else:
        bounding_box = None

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)

    cs_list, cs_metadata = get_contact_sites(
        cg,
        np.uint64(root_id),
        bounding_box=bounding_box,
        compute_partner=partners,
        time_stamp=timestamp,
    )

    return cs_list, cs_metadata


def handle_pairwise_contact_sites(table_id, first_node_id, second_node_id):
    current_app.request_type = "pairwise_contact_sites"
    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    timestamp = _parse_timestamp("timestamp", time.time(), return_datetime=True)

    exact_location = request.args.get("exact_location", True, type=app_utils.toboolean)
    cg = app_utils.get_cg(table_id)
    contact_sites_list, cs_metadata = contact_sites.get_contact_sites_pairwise(
        cg,
        np.uint64(first_node_id),
        np.uint64(second_node_id),
        end_time=timestamp,
        exact_location=exact_location,
    )
    return contact_sites_list, cs_metadata


### SPLIT PREVIEW --------------------------------------------------------------


def handle_split_preview(table_id):
    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    data = json.loads(request.data)
    current_app.logger.debug(data)

    cg = app_utils.get_cg(table_id)
    node_idents = []
    node_ident_map = {
        "sources": 0,
        "sinks": 1,
    }
    coords = []
    node_ids = []

    for k in ["sources", "sinks"]:
        for node in data[k]:
            node_ids.append(node[0])
            coords.append(np.array(node[1:]) / cg.segmentation_resolution)
            node_idents.append(node_ident_map[k])

    node_ids = np.array(node_ids, dtype=np.uint64)
    coords = np.array(coords)
    node_idents = np.array(node_idents)
    sv_ids = app_utils.handle_supervoxel_id_lookup(cg, coords, node_ids)
    current_app.logger.debug(
        {"node_id": node_ids, "sv_id": sv_ids, "node_ident": node_idents}
    )

    try:
        supervoxel_ccs, illegal_split = cutting.run_split_preview(
            cg=cg,
            source_ids=sv_ids[node_idents == 0],
            sink_ids=sv_ids[node_idents == 1],
            source_coords=coords[node_idents == 0],
            sink_coords=coords[node_idents == 1],
            bb_offset=(240, 240, 24),
        )
    except cg_exceptions.PreconditionError as e:
        raise cg_exceptions.BadRequest(str(e))

    resp = {
        "supervoxel_connected_components": supervoxel_ccs,
        "illegal_split": illegal_split,
    }
    return resp


### FIND PATH --------------------------------------------------------------


def handle_find_path(table_id, precision_mode):
    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    nodes = json.loads(request.data)
    current_app.logger.debug(nodes)
    assert len(nodes) == 2

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)
    node_ids = []
    coords = []
    for node in nodes:
        node_ids.append(node[0])
        coords.append(np.array(node[1:]) / cg.segmentation_resolution)

    if len(coords) != 2:
        cg_exceptions.BadRequest("Merge needs two nodes.")
    source_supervoxel_id, target_supervoxel_id = app_utils.handle_supervoxel_id_lookup(
        cg, coords, node_ids
    )

    source_l2_id = cg.get_parent(source_supervoxel_id)
    target_l2_id = cg.get_parent(target_supervoxel_id)

    print("Finding path...")
    print(f"Source: {source_supervoxel_id}")
    print(f"Target: {target_supervoxel_id}")

    root_time_stamp = cg.get_node_timestamps(
        [np.uint64(nodes[0][0])], return_numpy=False
    )[0]
    l2_path = pathing.find_l2_shortest_path(
        cg, source_l2_id, target_l2_id, time_stamp=root_time_stamp
    )
    print(f"Path: {l2_path}")
    if precision_mode:
        centroids, failed_l2_ids = mesh_analysis.compute_mesh_centroids_of_l2_ids(
            cg, l2_path, flatten=True
        )
        print(f"Centroids: {centroids}")
        print(f"Failed L2 ids: {failed_l2_ids}")
        return {
            "centroids_list": centroids,
            "failed_l2_ids": failed_l2_ids,
            "l2_path": l2_path,
        }
    else:
        centroids = pathing.compute_rough_coordinate_path(cg, l2_path)
        print(f"Centroids: {centroids}")
        return {"centroids_list": centroids, "failed_l2_ids": [], "l2_path": l2_path}


### GET_LAYER2_SUBGRAPH
def handle_get_layer2_graph(table_id, node_id):
    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id

    cg = app_utils.get_cg(table_id)
    print("Finding edge graph...")
    edge_graph = pathing.get_lvl2_edge_list(cg, int(node_id))
    print("Edge graph found len: {}".format(len(edge_graph)))
    return {"edge_graph": edge_graph}


### ROOT INFO ----------------------------------------------------------------


def handle_is_latest_roots(table_id, is_binary):
    current_app.request_type = "is_latest_roots"
    current_app.table_id = table_id

    if is_binary:
        node_ids = np.frombuffer(request.data, np.uint64)
    else:
        node_ids = np.array(json.loads(request.data)["node_ids"], dtype=np.uint64)
    # Convert seconds since epoch to UTC datetime
    timestamp = _parse_timestamp("timestamp", time.time(), return_datetime=True)

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)

    return cg.is_latest_roots(node_ids, time_stamp=timestamp)


def handle_root_timestamps(table_id, is_binary):
    current_app.request_type = "root_timestamps"
    current_app.table_id = table_id

    if is_binary:
        node_ids = np.frombuffer(request.data, np.uint64)
    else:
        node_ids = np.array(json.loads(request.data)["node_ids"], dtype=np.uint64)

    # Call ChunkedGraph
    cg = app_utils.get_cg(table_id)

    timestamps = cg.get_node_timestamps(node_ids, return_numpy=False)
    return [ts.timestamp() for ts in timestamps]


### OPERATION DETAILS ------------------------------------------------------------


def operation_details(table_id):
    from pychunkedgraph.graph import attributes
    from pychunkedgraph.export.operation_logs import parse_attr

    current_app.table_id = table_id
    user_id = str(g.auth_user["id"])
    current_app.user_id = user_id
    operation_ids = json.loads(request.args.get("operation_ids", "[]"))

    cg = app_utils.get_cg(table_id)
    log_rows = cg.client.read_log_entries(operation_ids)

    result = {}
    for k, v in log_rows.items():
        details = {}
        for _k, _v in v.items():
            _k, _v = parse_attr(_k, _v)
            try:
                details[_k.decode("utf-8")] = _v
            except AttributeError:
                details[_k] = _v
        result[int(k)] = details
    return result


### DELTA ROOTS ------------------------------------------------------------


def delta_roots(table_id):
    current_app.table_id = table_id

    timestamp_past = _parse_timestamp("timestamp_past", None, return_datetime=True)
    timestamp_future = _parse_timestamp(
        "timestamp_future", time.time(), return_datetime=True
    )
    cg = app_utils.get_cg(table_id)
    old_roots, new_roots = cg.get_proofread_root_ids(timestamp_past, timestamp_future)
    return {"old_roots": old_roots, "new_roots": new_roots}
