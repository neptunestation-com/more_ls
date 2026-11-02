"""Deliberate compiler mutations, used only by tests to prove that each template's differential
test can fail (invariant 3).  Production compiles use the empty set."""
from __future__ import annotations

from enum import Enum


class Mutation(str, Enum):
    CHECK_NAIVE = "check_naive"                      # CHECK (p) instead of CHECK ((p) IS TRUE)
    TRANSITION_NAIVE = "transition_naive"            # IF NOT (p) instead of IF NOT ((p) IS TRUE)
    FK_MATCH_FULL = "fk_match_full"                  # MATCH FULL instead of MATCH SIMPLE
    POLICY_ALL_RESTRICTIVE = "policy_all_restrictive"  # emit every policy as restrictive
    POLICY_DROP_RESTRICTIVE = "policy_drop_restrictive"  # omit restrictive policies
    COLUMN_TABLE_GRANT = "column_table_grant"        # table-level grant instead of column list
    TIME_NULL_CLOSED = "time_null_closed"            # NULL bound closes the interval
    INDEX_NONE = "index_none"                        # emit no indexes
    HARDENING_NO_FORCE = "hardening_no_force"        # skip FORCE ROW LEVEL SECURITY
    OPAQUE_INVERT = "opaque_invert"                  # negate opaque SQL
    MIGRATE_SKIP_VALIDATE = "migrate_skip_validate"  # leave migrated constraints NOT VALID
    HIERARCHY_SHALLOW = "hierarchy_shallow"          # reach one level less than max_depth
    HIERARCHY_NO_DEPTH_CHECK = "hierarchy_no_depth_check"  # omit CHECK (depth <= max_depth)
    CARDINALITY_OFF_BY_ONE = "cardinality_off_by_one"  # n > max instead of n + 1 > max
    CARDINALITY_NO_LOCK = "cardinality_no_lock"      # skip the parent-row lock (concurrency)
