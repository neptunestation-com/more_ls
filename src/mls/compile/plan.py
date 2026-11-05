"""Compile a checked spec into an ordered DDL script of fixed templates.

Sections, in order: preamble, roles, tables, checks, fks, transitions, hierarchies, cardinalities,
rls, policies, indexes, grants.  Before emitting, the compiler proves the soundness obligations that the targets need
(relation visibility: a membership subquery in a policy must not be filtered by RLS on the
relation's table) and refuses with an actionable error if any fails (invariant 4).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from mls.compile.cardinality import cardinality_ddl
from mls.compile.check import check_ddl
from mls.compile.column import grant_ddl, privileges, relations_in
from mls.compile.hierarchy import hierarchy_ddl
from mls.compile.internal import internal_policies, internal_role_ddl
from mls.compile.fk import fk_ddl
from mls.compile.hardening import preamble_ddl, role_ddl, table_hardening_ddl
from mls.compile.index import index_ddl
from mls.compile.mutations import Mutation
from mls.compile.policy import policy_ddl
from mls.compile.sql import SqlCompiler
from mls.compile.table import table_ddl
from mls.compile.transition import transition_ddl
from mls.errors import CompileError, MlsError
from mls.identity import get_adapter
from mls.props.effective import all_policies, all_transitions
from mls.spec.model import Spec


@dataclass
class Statement:
    section: str
    sql: str
    rule_id: str | None = None


@dataclass
class Script:
    statements: list[Statement] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    def add(self, section: str, sql: str | list[str] | None, rule_id: str | None = None) -> None:
        if sql is None:
            return
        for s in ([sql] if isinstance(sql, str) else sql):
            self.statements.append(Statement(section, s, rule_id))

    def sql(self) -> str:
        out = []
        section = None
        for st in self.statements:
            if st.section != section:
                section = st.section
                out.append(f"\n-- ==== {section} ====")
            tag = f"  -- {st.rule_id}" if st.rule_id else ""
            out.append(st.sql + ";" + tag)
        return "\n".join(out).strip() + "\n"

    def by_section(self, section: str) -> list[str]:
        return [s.sql for s in self.statements if s.section == section]


def soundness_errors(spec: Spec) -> list[MlsError]:
    """Obligations the compiler must discharge before emitting (see props/obligations.py)."""
    from mls.props.obligations import relation_visibility_obligations
    from mls.props.runner import run_obligation

    errors: list[MlsError] = []
    privs = privileges(spec)
    for i, p in enumerate(spec.policies()):
        rels = relations_in(p.using, set())
        relations_in(p.check, rels)
        for rname in sorted(rels):
            rel = spec.relations[rname]
            g = privs.get((p.role, rel.table), {}).get("select")
            if isinstance(g, set) and not {rel.key, rel.value} <= g:
                errors.append(MlsError(
                    path=f"$.rules[{spec.rules.index(p)}]",
                    message=f"policy '{p.name}' uses relation '{rname}' but a column rule limits "
                            f"role '{p.role}' to {sorted(g)} on '{rel.table}'; the membership "
                            f"subquery needs SELECT on ({rel.key}, {rel.value})",
                    fix=f"add '{rel.key}' and '{rel.value}' to the read column rule on '{rel.table}'",
                    code="compile"))
    for ob in relation_visibility_obligations(spec):
        res = run_obligation(spec, ob)
        if res.verdict != "PROVED":
            errors.append(MlsError(
                path=ob.path, message=ob.failure_message, fix=ob.fix, code="compile"))
    return errors


def compile_spec(spec: Spec, mutations: frozenset[Mutation] | set[Mutation] = frozenset(),
                 prove_soundness: bool = True) -> Script:
    mutations = frozenset(mutations)
    if prove_soundness:
        errs = soundness_errors(spec)
        if errs:
            raise CompileError(errs)
    adapter = get_adapter(spec)
    sc = SqlCompiler(spec, adapter, mutations)
    from mls import english

    s = Script(assumptions=[f"identity ({adapter.name}): {adapter.assumption}",
                            "trusted roles: the owner of the MLS schema and superusers",
                            *english.assumption_lines(spec)])
    s.add("preamble", preamble_ddl(spec))
    s.add("roles", role_ddl(spec))
    s.add("roles", internal_role_ddl(spec))
    for name, t in spec.tables.items():
        if t.mode == "owned":
            s.add("tables", table_ddl(name, t))  # adopted tables exist; observed ones are not touched
    for r in spec.checks():
        s.add("checks", check_ddl(r, sc), r.id)
    for r in spec.inclusions():
        s.add("fks", fk_ddl(r, sc), r.id)
    for r in all_transitions(spec):
        s.add("transitions", transition_ddl(r, sc), r.id)
    for name in spec.hierarchies:
        s.add("hierarchies", hierarchy_ddl(spec, name, sc), f"hierarchy:{name}")
    for r in spec.cardinalities():
        s.add("cardinalities", cardinality_ddl(spec, r, sc), r.id)
    s.add("rls", table_hardening_ddl(spec, mutations))
    for p in all_policies(spec) + internal_policies(spec):
        s.add("policies", policy_ddl(p, sc), p.id)
    if Mutation.INDEX_NONE not in mutations:
        s.add("indexes", index_ddl(spec))
    s.add("grants", grant_ddl(spec, mutations))
    return s
