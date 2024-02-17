from __future__ import annotations

import collections
from typing import Any
from typing import Iterable

import cudf as pd

from polars_api_compat.cudf.dataframe_object import LazyFrame
from polars_api_compat.spec import DataFrame as DataFrameT
from polars_api_compat.spec import GroupBy as GroupByT
from polars_api_compat.spec import IntoExpr
from polars_api_compat.spec import LazyFrame as LazyFrameT
from polars_api_compat.spec import LazyGroupBy as LazyGroupByT
from polars_api_compat.utils import parse_into_exprs


class GroupBy(GroupByT):
    def __init__(self, df: DataFrameT, keys: list[str], api_version: str) -> None:
        self._df = df
        self._keys = list(keys)
        self.api_version = api_version

    def agg(
        self,
        *aggs: IntoExpr | Iterable[IntoExpr],
        **named_aggs: IntoExpr,
    ) -> DataFrameT:
        return (
            LazyGroupBy(self._df.lazy(), self._keys, self.api_version)
            .agg(*aggs, **named_aggs)
            .collect()
        )


class LazyGroupBy(LazyGroupByT):
    def __init__(self, df: LazyFrameT, keys: list[str], api_version: str) -> None:
        self._df = df
        self._keys = list(keys)
        self.api_version = api_version

    def agg(
        self,
        *aggs: IntoExpr | Iterable[IntoExpr],
        **named_aggs: IntoExpr,
    ) -> LazyFrameT:
        exprs = parse_into_exprs(
            self._df.__lazyframe_namespace__(),
            *aggs,
            **named_aggs,
        )
        grouped = self._df.dataframe.groupby(
            list(self._keys),
            sort=False,
            as_index=False,
        )

        # Do some fastpaths, if possible
        new_cols: list[pd.DataFrame] = []
        to_remove: list[int] = []
        for i, expr in enumerate(exprs):
            if (
                expr.function_name is not None
                and expr.depth is not None
                and expr.depth <= 2
                # todo: this one shouldn't be necessary?
                and expr.root_names is not None
            ):
                # We must have a simple aggregation, such as
                #     .agg(mean=pl.col('a').mean())
                # or
                #     .agg(pl.col('a').mean())
                if expr.root_names is None or expr.output_names is None:
                    msg = "Unreachable code, please report a bug"
                    raise AssertionError(msg)
                if len(expr.root_names) != len(expr.output_names):
                    msg = "Unreachable code, please report a bug"
                    raise AssertionError(msg)
                new_names = dict(zip(expr.root_names, expr.output_names))
                try:
                    new_cols.append(
                        getattr(grouped[expr.root_names], expr.function_name)()[
                            expr.root_names
                        ].rename(columns=new_names),
                    )
                except Exception as exp:  # noqa: BLE001
                    msg = f"Failed to apply fastpath for {expr}: {exp}"
                    print(msg)  # noqa: T201
                else:
                    to_remove.append(i)
        exprs = [expr for i, expr in enumerate(exprs) if i not in to_remove]

        out: dict[str, list[Any]] = collections.defaultdict(list)
        for key, _df in grouped:
            for _key, _name in zip(key, self._keys):
                out[_name].append(_key)
            for expr in exprs:
                result = expr.call(LazyFrame(_df, api_version=self.api_version))
                for _result in result:
                    _series = _result.series
                    assert len(_series) == 1
                    out[_result.name].append(_series.iloc[0])
        result = pd.DataFrame(out)
        result = pd.concat([result, *new_cols], axis=1)
        return LazyFrame(result, api_version=self.api_version)