#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from typing import NamedTuple, Optional, Tuple

from cmk.utils.check_utils import ServiceCheckResult


class AggregatedResult(NamedTuple):
    submit: bool
    data_received: bool
    result: ServiceCheckResult
    cache_info: Optional[Tuple[int, int]]
