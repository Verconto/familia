"""Tag-based access control layer for familia.

The package separates the structural data (graphs, schema, codec) from
the runtime ACL hooks (tag_check, vocabulary). Modules:

* :mod:`familia.acl.schema` — frozen dataclasses for graphs and wrapped
  records. Constants: ``WRAP_SENTINEL`` (the version marker that
  distinguishes a tag-wrapped value from a legacy unwrapped one).
* :mod:`familia.acl.codec` — encode/decode of wrapped record bodies. The
  decoder is fail-closed: anything that doesn't match the sentinel + tags
  + value shape is treated as a legacy untagged string.
* :mod:`familia.acl.reachable` — pure resolver. Given an actor and the two
  graphs, returns the set of tag-ids the actor can reach. Honors
  ``role: child`` asymmetry on ``parent_of`` (parent sees child; child
  does not see parent).

Runtime ACL enforcement (memory_get/set, cron list, vocabulary injection)
lives in the tools/ that consume these helpers — they import from this
package and call the pure functions.
"""

from familia.acl.codec import decode, encode
from familia.acl.schema import (
    WRAP_SENTINEL,
    WRAP_SENTINEL_KEY,
    Graph,
    GraphEdge,
    GraphNode,
    WrappedRecord,
)

__all__ = [
    "WRAP_SENTINEL",
    "WRAP_SENTINEL_KEY",
    "Graph",
    "GraphEdge",
    "GraphNode",
    "WrappedRecord",
    "decode",
    "encode",
]
