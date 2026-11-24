"""Real native Parquet fixtures cross the bounded worker boundary."""
from datetime import date
import hashlib

import pyarrow as pa
import pyarrow.parquet as pq

from bulk_common import Blob, Err, Ok, load_json
from joined_worker import project_file

CONFIG = {"usc_title": "18", "sections": ["111", "841"], "court_types": ["F", "FD"], "since": None}


def make_parquet(path, rows, row_group_size=1):
    opinion = pa.struct([("opinion_id", pa.int32()), ("opinion_text", pa.string()), ("type", pa.string()), ("ocr", pa.bool_())])
    schema = pa.schema([
        ("id", pa.int32()), ("case_name", pa.string()), ("date_filed", pa.date32()),
        ("date_filed_is_approximate", pa.bool_()), ("court_type", pa.string()),
        ("court_jurisdiction", pa.string()), ("court_full_name", pa.string()),
        ("court_short_name", pa.string()), ("citations", pa.list_(pa.string())),
        ("opinions", pa.list_(opinion)),
    ])
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path, row_group_size=row_group_size)
    return Blob.of(path.read_bytes())


def row(case_id=1, opinion_id=2, text="🦉 18 U.S.C. § 111(a).", **updates):
    return {"id": case_id, "case_name": "Fixture", "date_filed": date(2020, 1, 1),
            "date_filed_is_approximate": False, "court_type": "F", "court_jurisdiction": "USA, Federal",
            "court_full_name": "United States Court of Appeals", "court_short_name": "Fixture",
            "citations": ["1 F.4th 1"], "opinions": [{"opinion_id": opinion_id, "opinion_text": text, "type": "020lead", "ocr": False}], **updates}


def records(directory, product):
    return [load_json(line) for partition in product["partitions"]
            for line in (directory / partition["blob"]["sha256"]).read_bytes().splitlines()]


def test_real_parquet_native_dates_unicode_locators_and_complete_zero_match_groups(tmp_path):
    source = tmp_path / "source.parquet"
    blob = make_parquet(source, [row(), row(3, 4, "26 U.S.C. § 111."), row(5, 6, court_type="SA", court_jurisdiction="California", court_full_name="Supreme Court of California")])
    output = tmp_path / "out"
    result = project_file(source, blob, CONFIG, output)
    assert isinstance(result, Ok), result
    product = result.value
    assert product["coverage"]["records_scanned"] == 3
    assert product["coverage"]["records_selected"] == 2
    assert product["coverage"]["records_with_matches"] == 1
    assert product["coverage"]["global_corpus_complete"] is False
    evidence = records(output, product)
    assert len(evidence) == 1
    match = evidence[0]
    text = row()["opinions"][0]["opinion_text"]
    assert match["kind"] == "match"
    assert (match["row_group"], match["row_in_group"], match["source_row"], match["opinion_index"]) == (0, 0, 0, 0)
    assert text[match["section_span"]["start"]:match["section_span"]["end"]] == "111(a)"
    assert text[match["citation_span"]["start"]:match["citation_span"]["end"]] == match["citation_text"]
    assert match["text_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert match["date_filed"] == "2020-01-01"
    assert match["section"] == "111" and match["subsection"] == "(a)"
    assert match["editorial_review"] == "unreviewed"


def test_null_collections_ids_dates_are_unresolved_and_since_never_invents_negative(tmp_path):
    source = tmp_path / "source.parquet"
    blob = make_parquet(source, [row(opinions=None), row(3, None), row(4, 5, date_filed=None), row(6, 7, court_type=None)])
    config = {**CONFIG, "since": "2019-01-01"}
    output = tmp_path / "out"
    result = project_file(source, blob, config, output)
    assert isinstance(result, Ok), result
    product = result.value
    reasons = {item["reason"] for item in records(output, product)}
    assert {"unavailable_opinion_collection", "missing_opinion_id", "unknown_source_date", "unknown_court_type"} <= reasons
    assert product["coverage"]["selected_opinions_complete"] is False
    assert product["coverage"]["records_scanned"] == 4
    assert product["coverage"]["records_unknown_metadata"] == 4
    missing_id = next(item for item in records(output, product) if item["reason"] == "missing_opinion_id")
    assert missing_id["opinion_id"] is None


def test_duplicate_identity_is_counted_and_contradiction_fails_closed(tmp_path):
    source = tmp_path / "source.parquet"
    blob = make_parquet(source, [row(), row()])
    result = project_file(source, blob, CONFIG, tmp_path / "good")
    assert isinstance(result, Ok), result
    assert result.value["coverage"]["records_duplicate"] == 1
    blob = make_parquet(source, [row(), row(text="contradiction")])
    assert isinstance(project_file(source, blob, CONFIG, tmp_path / "bad"), Err)


def test_malformed_footer_wrong_hash_and_output_budget_fail(tmp_path):
    source = tmp_path / "source.parquet"
    source.write_bytes(b"not parquet")
    assert isinstance(project_file(source, Blob.of(source.read_bytes()), CONFIG, tmp_path / "bad"), Err)
    blob = make_parquet(source, [row()])
    assert isinstance(project_file(source, Blob("0" * 64, blob.byte_length), CONFIG, tmp_path / "wrong"), Err)
    assert isinstance(project_file(source, blob, CONFIG, tmp_path / "small", limits={"output_bytes": 8}), Err)


def test_partition_bytes_are_deterministic_across_reader_batch_sizes(tmp_path):
    source = tmp_path / "source.parquet"
    blob = make_parquet(source, [row(i, i + 100) for i in range(1, 7)], 4)
    first = project_file(source, blob, CONFIG, tmp_path / "a", limits={"batch_rows": 1, "partition_bytes": 2200})
    second = project_file(source, blob, CONFIG, tmp_path / "b", limits={"batch_rows": 3, "partition_bytes": 2200})
    assert isinstance(first, Ok), first
    assert isinstance(second, Ok), second
    assert first.value == second.value
    assert len(first.value["partitions"]) >= 3


def test_bounded_worker_rejects_memory_exhaustion_and_oversized_text(tmp_path):
    source = tmp_path / "source.parquet"
    blob = make_parquet(source, [row(text="x" * (5 * 1024**2))])
    assert isinstance(project_file(source, blob, CONFIG, tmp_path / "large-text"), Err)
    assert isinstance(project_file(source, blob, CONFIG, tmp_path / "low-memory", limits={"address_bytes": 48 * 1024**2}), Err)


def test_actual_worker_wall_deadline_and_sqlite_budget_fail_closed(tmp_path):
    source = tmp_path / "source.parquet"
    blob = make_parquet(source, [row(i, i + 10000, "plain " * 600) for i in range(1, 10001)], 1000)
    deadline = project_file(source, blob, CONFIG, tmp_path / "deadline", limits={"wall_seconds": 1})
    assert isinstance(deadline, Err)
    assert "time budget" in deadline.error
    index = project_file(source, blob, CONFIG, tmp_path / "index", limits={"index_bytes": 4096})
    assert isinstance(index, Err)
    assert "full" in index.error.lower() or "index" in index.error.lower()


def test_worker_observes_persistent_reserve_before_output_or_index_growth(tmp_path):
    source = tmp_path / "source.parquet"
    blob = make_parquet(source, [row()])
    result = project_file(source, blob, CONFIG, tmp_path / "reserve", limits={"min_free_bytes": 1024**4})
    assert isinstance(result, Err)
    assert "reserve" in result.error


def test_missing_case_and_reporter_metadata_retain_unknown_observations(tmp_path):
    source = tmp_path / "source.parquet"
    blob = make_parquet(source, [row(None, 2), row(3, 4, citations=None)])
    output = tmp_path / "unknown"
    result = project_file(source, blob, CONFIG, output)
    assert isinstance(result, Ok), result
    unknown = records(output, result.value)
    assert {"missing_case_id", "invalid_case_citations"} <= {item.get("reason") for item in unknown}
    assert next(item for item in unknown if item.get("reason") == "missing_case_id")["case_id"] is None
