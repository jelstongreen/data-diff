from dataclasses import field
from datetime import datetime
from typing import Any, Generator, Optional, Sequence, Tuple, Union

from runtype import dataclass

from data_diff.utils import ArithString, join_iter

from .compiler import Compilable, Compiler
from .base import SKIP, CompileError, DbPath, Schema, args_as_tuple


class ExprNode(Compilable):
    type: Any = None

    def _dfs_values(self):
        yield self
        for k, vs in dict(self).items():  # __dict__ provided by runtype.dataclass
            if k == "source_table":
                # Skip data-sources, we're only interested in data-parameters
                continue
            if not isinstance(vs, (list, tuple)):
                vs = [vs]
            for v in vs:
                if isinstance(v, ExprNode):
                    yield from v._dfs_values()

    def cast_to(self, to):
        return Cast(self, to)


Expr = Union[ExprNode, str, bool, int, datetime, ArithString, None]


def get_type(e: Expr) -> type:
    if isinstance(e, ExprNode):
        return e.type
    return type(e)


@dataclass
class Alias(ExprNode):
    expr: Expr
    name: str

    def compile(self, c: Compiler) -> str:
        return f"{c.compile(self.expr)} AS {c.quote(self.name)}"

    @property
    def type(self):
        return get_type(self.expr)


def _drop_skips(exprs):
    return [e for e in exprs if e is not SKIP]


def _drop_skips_dict(exprs_dict):
    return {k: v for k, v in exprs_dict.items() if v is not SKIP}


class ITable:
    source_table: Any
    schema: Schema = None

    def select(self, *exprs, **named_exprs):
        exprs = args_as_tuple(exprs)
        exprs = _drop_skips(exprs)
        named_exprs = _drop_skips_dict(named_exprs)
        exprs += _named_exprs_as_aliases(named_exprs)
        resolve_names(self.source_table, exprs)
        return Select.make(self, columns=exprs)

    def where(self, *exprs):
        exprs = args_as_tuple(exprs)
        exprs = _drop_skips(exprs)
        if not exprs:
            return self

        resolve_names(self.source_table, exprs)
        return Select.make(self, where_exprs=exprs, _concat=True)

    def order_by(self, *exprs):
        exprs = _drop_skips(exprs)
        if not exprs:
            return self

        resolve_names(self.source_table, exprs)
        return Select.make(self, order_by_exprs=exprs)

    def limit(self, limit: int):
        if limit is SKIP:
            return self

        return Select.make(self, limit_expr=limit)

    def at(self, *exprs):
        # TODO
        exprs = _drop_skips(exprs)
        if not exprs:
            return self

        raise NotImplementedError()

    def join(self, target):
        return Join(self, target)

    def group_by(self, *, keys=None, values=None):
        # TODO
        assert keys or values
        raise NotImplementedError()

    def with_schema(self):
        # TODO
        raise NotImplementedError()

    def _get_column(self, name: str):
        if self.schema:
            name = self.schema.get_key(name)  # Get the actual name. Might be case-insensitive.
        return Column(self, name)

    # def __getattr__(self, column):
    #     return self._get_column(column)

    def __getitem__(self, column):
        if not isinstance(column, str):
            raise TypeError()
        return self._get_column(column)

    def count(self):
        return Select(self, [Count()])

    def union(self, other: "ITable"):
        return Union(self, other)


@dataclass
class Concat(ExprNode):
    exprs: list
    sep: str = None

    def compile(self, c: Compiler) -> str:
        # We coalesce because on some DBs (e.g. MySQL) concat('a', NULL) is NULL
        items = [f"coalesce({c.compile(c.database.to_string(c.compile(expr)))}, '<null>')" for expr in self.exprs]
        assert items
        if len(items) == 1:
            return items[0]

        if self.sep:
            items = list(join_iter(f"'{self.sep}'", items))
        return c.database.concat(items)


@dataclass
class Count(ExprNode):
    expr: Expr = "*"
    distinct: bool = False

    def compile(self, c: Compiler) -> str:
        expr = c.compile(self.expr)
        if self.distinct:
            return f"count(distinct {expr})"

        return f"count({expr})"


@dataclass
class Func(ExprNode):
    name: str
    args: Sequence[Expr]

    def compile(self, c: Compiler) -> str:
        args = ", ".join(c.compile(e) for e in self.args)
        return f"{self.name}({args})"


def _expr_type(e: Expr):
    if isinstance(e, ExprNode):
        return e.type
    return type(e)


@dataclass
class CaseWhen(ExprNode):
    cases: Sequence[Tuple[Expr, Expr]]
    else_: Expr = None

    def compile(self, c: Compiler) -> str:
        assert self.cases
        when_thens = " ".join(f"WHEN {c.compile(when)} THEN {c.compile(then)}" for when, then in self.cases)
        else_ = (" ELSE " + c.compile(self.else_)) if self.else_ is not None else ""
        return f"CASE {when_thens}{else_} END"

    @property
    def type(self):
        when_types = {_expr_type(w) for _c, w in self.cases}
        if self.else_:
            when_types |= _expr_type(self.else_)
        if len(when_types) > 1:
            raise RuntimeError(f"Non-matching types in when: {when_types}")
        (t,) = when_types
        return t


class LazyOps:
    def __add__(self, other):
        return BinOp("+", [self, other])

    def __gt__(self, other):
        return BinBoolOp(">", [self, other])

    def __ge__(self, other):
        return BinBoolOp(">=", [self, other])

    def __eq__(self, other):
        if other is None:
            return BinBoolOp("IS", [self, None])
        return BinBoolOp("=", [self, other])

    def __lt__(self, other):
        return BinBoolOp("<", [self, other])

    def __le__(self, other):
        return BinBoolOp("<=", [self, other])

    def __or__(self, other):
        return BinBoolOp("OR", [self, other])

    def is_distinct_from(self, other):
        return IsDistinctFrom(self, other)

    def sum(self):
        return Func("SUM", [self])


@dataclass(eq=False, order=False)
class IsDistinctFrom(ExprNode, LazyOps):
    a: Expr
    b: Expr
    type = bool

    def compile(self, c: Compiler) -> str:
        return c.database.is_distinct_from(c.compile(self.a), c.compile(self.b))


@dataclass(eq=False, order=False)
class BinOp(ExprNode, LazyOps):
    op: str
    args: Sequence[Expr]

    def __post_init__(self):
        assert len(self.args) == 2, self.args

    def compile(self, c: Compiler) -> str:
        a, b = self.args
        return f"({c.compile(a)} {self.op} {c.compile(b)})"


class BinBoolOp(BinOp):
    type = bool


@dataclass(eq=False, order=False)
class Column(ExprNode, LazyOps):
    source_table: ITable
    name: str

    @property
    def type(self):
        if self.source_table.schema is None:
            raise RuntimeError(f"Schema required for table {self.source_table}")
        return self.source_table.schema[self.name]

    def compile(self, c: Compiler) -> str:
        if c._table_context:
            if len(c._table_context) > 1:
                aliases = [
                    t for t in c._table_context if isinstance(t, TableAlias) and t.source_table is self.source_table
                ]
                if not aliases:
                    return c.quote(self.name)
                elif len(aliases) > 1:
                    raise CompileError(f"Too many aliases for column {self.name}")
                (alias,) = aliases

                return f"{c.quote(alias.name)}.{c.quote(self.name)}"

        return c.quote(self.name)


@dataclass
class TablePath(ExprNode, ITable):
    path: DbPath
    schema: Optional[Schema] = field(default=None, repr=False)

    def create(self, if_not_exists=False):
        if not self.schema:
            raise ValueError("Schema must have a value to create table")
        return CreateTable(self, if_not_exists=if_not_exists)

    def drop(self, if_exists=False):
        return DropTable(self, if_exists=if_exists)

    def insert_values(self, rows):
        raise NotImplementedError()

    def insert_expr(self, expr: Expr):
        return InsertToTable(self, expr)

    @property
    def source_table(self):
        return self

    def compile(self, c: Compiler) -> str:
        path = self.path  # c.database._normalize_table_path(self.name)
        return ".".join(map(c.quote, path))


@dataclass
class TableAlias(ExprNode, ITable):
    source_table: ITable
    name: str

    def compile(self, c: Compiler) -> str:
        return f"{c.compile(self.source_table)} {c.quote(self.name)}"


@dataclass
class Join(ExprNode, ITable):
    source_tables: Sequence[ITable]
    op: str = None
    on_exprs: Sequence[Expr] = None
    columns: Sequence[Expr] = None

    @property
    def source_table(self):
        return self  # TODO is this right?

    @property
    def schema(self):
        assert self.columns  # TODO Implement SELECT *
        s = self.source_tables[0].schema  # XXX
        return type(s)({c.name: c.type for c in self.columns})

    def on(self, *exprs):
        if len(exprs) == 1:
            (e,) = exprs
            if isinstance(e, Generator):
                exprs = tuple(e)

        exprs = _drop_skips(exprs)
        if not exprs:
            return self

        return self.replace(on_exprs=(self.on_exprs or []) + exprs)

    def select(self, *exprs, **named_exprs):
        if self.columns is not None:
            # join-select already applied
            return super().select(*exprs, **named_exprs)

        exprs = _drop_skips(exprs)
        named_exprs = _drop_skips_dict(named_exprs)
        exprs += _named_exprs_as_aliases(named_exprs)
        # resolve_names(self.source_table, exprs)
        # TODO Ensure exprs <= self.columns ?
        return self.replace(columns=exprs)

    def compile(self, parent_c: Compiler) -> str:
        tables = [
            t if isinstance(t, TableAlias) else TableAlias(t, parent_c.new_unique_name()) for t in self.source_tables
        ]
        c = parent_c.add_table_context(*tables).replace(in_join=True, in_select=False)
        op = " JOIN " if self.op is None else f" {self.op} JOIN "
        joined = op.join(c.compile(t) for t in tables)

        if self.on_exprs:
            on = " AND ".join(c.compile(e) for e in self.on_exprs)
            res = f"{joined} ON {on}"
        else:
            res = joined

        columns = "*" if self.columns is None else ", ".join(map(c.compile, self.columns))
        select = f"SELECT {columns} FROM {res}"

        if parent_c.in_select:
            select = f"({select}) {c.new_unique_name()}"
        elif parent_c.in_join:
            select = f"({select})"
        return select


class GroupBy(ITable):
    def having(self):
        pass


@dataclass
class Union(ExprNode, ITable):
    table1: ITable
    table2: ITable

    @property
    def source_table(self):
        return self  # TODO is this right?

    @property
    def type(self):
        return self.table1.type

    @property
    def schema(self):
        s1 = self.table1.schema
        s2 = self.table2.schema
        assert len(s1) == len(s2)
        return s1

    def compile(self, parent_c: Compiler) -> str:
        c = parent_c.replace(in_select=False)
        union_all = f"{c.compile(self.table1)} UNION {c.compile(self.table2)}"
        if parent_c.in_select:
            union_all = f"({union_all}) {c.new_unique_name()}"
        elif parent_c.in_join:
            union_all = f"({union_all})"
        return union_all


@dataclass
class Select(ExprNode, ITable):
    table: Expr = None
    columns: Sequence[Expr] = None
    where_exprs: Sequence[Expr] = None
    order_by_exprs: Sequence[Expr] = None
    group_by_exprs: Sequence[Expr] = None
    limit_expr: int = None

    @property
    def schema(self):
        s = self.table.schema
        if s is None or self.columns is None:
            return s
        return type(s)({c.name: c.type for c in self.columns})

    @property
    def source_table(self):
        return self

    def compile(self, parent_c: Compiler) -> str:
        c = parent_c.replace(in_select=True)  # .add_table_context(self.table)

        columns = ", ".join(map(c.compile, self.columns)) if self.columns else "*"
        select = f"SELECT {columns}"

        if self.table:
            select += " FROM " + c.compile(self.table)

        if self.where_exprs:
            select += " WHERE " + " AND ".join(map(c.compile, self.where_exprs))

        if self.group_by_exprs:
            select += " GROUP BY " + ", ".join(map(c.compile, self.group_by_exprs))

        if self.order_by_exprs:
            select += " ORDER BY " + ", ".join(map(c.compile, self.order_by_exprs))

        if self.limit_expr is not None:
            select += " " + c.database.offset_limit(0, self.limit_expr)

        if parent_c.in_select:
            select = f"({select}) {c.new_unique_name()}"
        elif parent_c.in_join:
            select = f"({select})"
        return select

    @classmethod
    def make(cls, table: ITable, _concat: bool = False, **kwargs):
        if not isinstance(table, cls):
            return cls(table, **kwargs)

        # Fill in missing attributes, instead of creating a new instance.
        for k, v in kwargs.items():
            if getattr(table, k) is not None:
                if _concat:
                    kwargs[k] = getattr(table, k) + v
                else:
                    raise ValueError("...")

        return table.replace(**kwargs)


@dataclass
class Cte(ExprNode, ITable):
    source_table: Expr
    name: str = None
    params: Sequence[str] = None

    def compile(self, parent_c: Compiler) -> str:
        c = parent_c.replace(_table_context=[], in_select=False)
        compiled = c.compile(self.source_table)

        name = self.name or parent_c.new_unique_name()
        name_params = f"{name}({', '.join(self.params)})" if self.params else name
        parent_c._subqueries[name_params] = compiled

        return name

    @property
    def schema(self):
        # TODO add cte to schema
        return self.source_table.schema


def _named_exprs_as_aliases(named_exprs):
    return [Alias(expr, name) for name, expr in named_exprs.items()]


def resolve_names(source_table, exprs):
    i = 0
    for expr in exprs:
        # Iterate recursively and update _ResolveColumn with the right expression
        if isinstance(expr, ExprNode):
            for v in expr._dfs_values():
                if isinstance(v, _ResolveColumn):
                    v.resolve(source_table._get_column(v.resolve_name))
                    i += 1


@dataclass(frozen=False, eq=False, order=False)
class _ResolveColumn(ExprNode, LazyOps):
    resolve_name: str
    resolved: Expr = None

    def resolve(self, expr):
        assert self.resolved is None
        self.resolved = expr

    def compile(self, c: Compiler) -> str:
        if self.resolved is None:
            raise RuntimeError(f"Column not resolved: {self.resolve_name}")
        return self.resolved.compile(c)

    @property
    def type(self):
        if self.resolved is None:
            raise RuntimeError(f"Column not resolved: {self.resolve_name}")
        return self.resolved.type

    @property
    def name(self):
        if self.resolved is None:
            raise RuntimeError(f"Column not resolved: {self.name}")
        return self.resolved.name


class This:
    def __getattr__(self, name):
        return _ResolveColumn(name)

    def __getitem__(self, name):
        if isinstance(name, list):
            return [_ResolveColumn(n) for n in name]
        return _ResolveColumn(name)


@dataclass
class Explain(ExprNode):
    sql: Select

    def compile(self, c: Compiler) -> str:
        return f"EXPLAIN {c.compile(self.sql)}"


@dataclass
class In(ExprNode):
    expr: Expr
    list: Sequence[Expr]

    def compile(self, c: Compiler):
        elems = ", ".join(map(c.compile, self.list))
        return f"({c.compile(self.expr)} IN ({elems}))"


@dataclass
class Cast(ExprNode):
    expr: Expr
    target_type: Expr

    def compile(self, c: Compiler) -> str:
        return f"cast({c.compile(self.expr)} as {c.compile(self.target_type)})"


@dataclass
class Random(ExprNode):
    def compile(self, c: Compiler) -> str:
        return c.database.random()


# DDL


class Statement(Compilable):
    type = None


@dataclass
class CreateTable(Statement):
    path: TablePath
    if_not_exists: bool = False

    def compile(self, c: Compiler) -> str:
        schema = ", ".join(f"{k} {c.database.type_repr(v)}" for k, v in self.path.schema.items())
        ne = "IF NOT EXISTS " if self.if_not_exists else ""
        return f"CREATE TABLE {ne}{c.compile(self.path)}({schema})"


@dataclass
class DropTable(Statement):
    path: TablePath
    if_exists: bool = False

    def compile(self, c: Compiler) -> str:
        ie = "IF EXISTS " if self.if_exists else ""
        return f"DROP TABLE {ie}{c.compile(self.path)}"


@dataclass
class InsertToTable(Statement):
    # TODO Support insert for only some columns
    path: TablePath
    expr: Expr

    def compile(self, c: Compiler) -> str:
        return f"INSERT INTO {c.compile(self.path)} {c.compile(self.expr)}"


@dataclass
class Commit(Statement):
    def compile(self, c: Compiler) -> str:
        return "COMMIT" if not c.database.is_autocommit else SKIP