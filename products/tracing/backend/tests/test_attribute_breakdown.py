import datetime as dt

from posthog.clickhouse.client import sync_execute

from products.tracing.backend.tests.test_keyset_pagination import DATE_FROM, DATE_TO, _b64, _TraceSpansTestBase

# CDP-style outbound POST spans hitting two destinations. Amplitude is the broken one —
# 4 of its 5 spans are errors — so a breakdown over `server.address` should surface it
# as the over-represented value among the bad spans. One span carries no attribute at
# all (groups under ''), and one lives in another service (excluded by serviceNames).
MS = 1_000_000  # ns per ms
SPANS = [
    # (service, server.address, k8s.pod.name, status_code, duration)
    ("cdp", "api2.amplitude.com", "pod-a", 2, dt.timedelta(milliseconds=100)),
    ("cdp", "api2.amplitude.com", "pod-a", 2, dt.timedelta(milliseconds=100)),
    ("cdp", "api2.amplitude.com", "pod-a", 2, dt.timedelta(milliseconds=100)),
    ("cdp", "api2.amplitude.com", "pod-a", 2, dt.timedelta(milliseconds=100)),
    ("cdp", "api2.amplitude.com", "pod-a", 0, dt.timedelta(milliseconds=100)),
    ("cdp", "api.mixpanel.com", "pod-b", 0, dt.timedelta(milliseconds=200)),
    ("cdp", "api.mixpanel.com", "pod-b", 0, dt.timedelta(milliseconds=200)),
    ("cdp", "api.mixpanel.com", "pod-b", 0, dt.timedelta(milliseconds=200)),
    ("cdp", None, "pod-b", 0, dt.timedelta(milliseconds=300)),
    ("other", "api2.amplitude.com", "pod-c", 0, dt.timedelta(milliseconds=100)),
]


class TestTraceSpansAttributeBreakdown(_TraceSpansTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls._recreate_trace_spans_tables()

        base = dt.datetime(2026, 6, 2, 8, 0, 0)
        rows: list[str] = []
        for i, (service, address, pod, status_code, duration) in enumerate(SPANS):
            trace_id = _b64(i.to_bytes(16, "big"))
            span_id = _b64((1000 + i).to_bytes(8, "big"))
            ts_str = base.strftime("%Y-%m-%d %H:%M:%S.%f")
            end_str = (base + duration).strftime("%Y-%m-%d %H:%M:%S.%f")
            attributes = f"map('server.address__str', '{address}')" if address else "map()"
            rows.append(
                f"('019e8758-0000-0000-0000-{i:012d}', {cls.team.id}, '{trace_id}', '{span_id}', '', "
                f"'POST', 3, '{ts_str}', '{end_str}', '{ts_str}', {status_code}, '{service}', "
                f"{attributes}, map('k8s.pod.name', '{pod}'))"
            )
        sync_execute(
            "INSERT INTO trace_spans (uuid, team_id, trace_id, span_id, parent_span_id, name, kind, "
            "timestamp, end_time, observed_timestamp, status_code, service_name, attributes_map_str, "
            "resource_attributes) VALUES " + ",".join(rows)
        )

    def _breakdown(self, **query_fields) -> list[dict]:
        query: dict = {"dateRange": {"date_from": DATE_FROM, "date_to": DATE_TO}, **query_fields}
        response = self.client.post(
            f"/api/projects/{self.team.id}/tracing/spans/attribute-breakdown/",
            {"query": query},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["results"]

    def test_groups_by_span_attribute_value(self):
        rows = self._breakdown(breakdownKey="server.address", breakdownType="span_attribute")
        by_value = {row["value"]: row for row in rows}
        self.assertEqual(
            {(v, r["count"], r["error_count"]) for v, r in by_value.items()},
            {
                ("api2.amplitude.com", 6, 4),
                ("api.mixpanel.com", 3, 0),
                ("", 1, 0),
            },
        )
        # Default ordering is count DESC.
        self.assertEqual(rows[0]["value"], "api2.amplitude.com")
        # All amplitude spans run 100ms, so both quantiles sit on the constant.
        self.assertEqual(by_value["api2.amplitude.com"]["p50_duration_nano"], 100 * MS)
        self.assertEqual(by_value["api2.amplitude.com"]["p95_duration_nano"], 100 * MS)

    def test_groups_by_resource_attribute_value(self):
        rows = self._breakdown(breakdownKey="k8s.pod.name", breakdownType="span_resource_attribute")
        self.assertEqual(
            {(row["value"], row["count"]) for row in rows},
            {("pod-a", 5), ("pod-b", 4), ("pod-c", 1)},
        )

    def test_order_by_error_count(self):
        rows = self._breakdown(breakdownKey="server.address", breakdownType="span_attribute", orderBy="error_count")
        self.assertEqual(rows[0]["value"], "api2.amplitude.com")
        self.assertEqual(rows[0]["error_count"], 4)

    def test_service_names_scope_the_breakdown(self):
        rows = self._breakdown(breakdownKey="server.address", breakdownType="span_attribute", serviceNames=["cdp"])
        by_value = {row["value"]: row["count"] for row in rows}
        self.assertEqual(by_value["api2.amplitude.com"], 5)

    def test_filter_group_scopes_the_breakdown(self):
        # The BubbleUp shape: breakdown of only the BAD spans.
        rows = self._breakdown(
            breakdownKey="server.address",
            breakdownType="span_attribute",
            filterGroup=[{"key": "status_code", "operator": "exact", "type": "span", "value": "Error"}],
        )
        self.assertEqual(
            {(row["value"], row["count"]) for row in rows},
            {("api2.amplitude.com", 4)},
        )
