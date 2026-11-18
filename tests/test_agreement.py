import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from meridian.api import create_app
from meridian.demo import decision, observation, query, source
from tests.common import ServiceCase


def both_regions():
    return query() | {"regions": ["North", "South"]}


def ruling(external_id, stance="mixed", adjudicator="carol"):
    return {"origin": "local-fixture", "external_id": external_id, "revision": 1,
            "adjudicator": adjudicator, "topic": "technology", "stance": stance,
            "audience": "general", "reason": "third reviewer reading of the same item"}


class StrataTests(ServiceCase):
    def sources_by_id(self, strata):
        return {row["source_id"]: row for row in strata["sources"]}

    def test_strata_keep_story_denominators_for_each_state(self):
        self.accepted("a", "north-a", "shared")
        self.accepted("b", "north-b", "shared")
        self.add("c", "north-a", "c-story")
        self.service.coding.submit(decision("c", "alice", "positive"))
        self.service.coding.submit(decision("c", "bob", "negative"))
        self.add("d", "north-b", "d-story")
        self.service.coding.submit(decision("d", "alice", "positive"))
        self.service.coding.submit(decision("d", "bob", "negative"))
        self.service.coding.adjudicate(ruling("d"))
        self.add("e", "north-a", "e-story")
        strata = self.service.agreement.strata(query())
        by_source = self.sources_by_id(strata)
        north_a = by_source["north-a"]
        self.assertEqual(north_a["observations"], 3)
        self.assertEqual(north_a["states"]["consensus"], {"observations": 1, "stories": 1})
        self.assertEqual(north_a["states"]["disputed"], {"observations": 1, "stories": 1})
        self.assertEqual(north_a["states"]["adjudicated"], {"observations": 0, "stories": 0})
        self.assertEqual(north_a["states"]["uncoded"], {"observations": 1, "stories": 1})
        self.assertEqual(north_a["coded"], {"observations": 2, "stories": 2})
        self.assertEqual(north_a["pairwise_agreement"], 0.5)
        north_b = by_source["north-b"]
        self.assertEqual(north_b["states"]["adjudicated"], {"observations": 1, "stories": 1})
        self.assertEqual(north_b["coded"], {"observations": 2, "stories": 2})
        self.assertEqual(north_b["pairwise_agreement"], 0.5)
        self.assertNotIn("south-a", by_source)
        region = strata["regions"][0]
        self.assertEqual(region["region"], "North")
        self.assertEqual(region["observations"], 5)
        self.assertEqual(region["stories"], 4)
        self.assertEqual(region["coded"], {"observations": 4, "stories": 3})
        report = self.report()
        self.assertEqual(strata["observations_in_window"], report["observations_in_window"])
        self.assertEqual(sum(row["observations"] for row in strata["topics"]), 5)
        self.assertEqual(sum(row["observations"] for row in strata["cells"]), 5)
        cells = {(row["source_id"], row["topic"]): row for row in strata["cells"]}
        self.assertEqual(cells[("north-a", "technology")]["coded"]["observations"], 2)
        self.assertEqual(cells[("north-a", "undetermined")]["states"]["uncoded"]["observations"], 1)

    def test_shared_story_counts_each_outlets_own_evidence(self):
        self.accepted("a", "north-a", "shared")
        self.add("b", "north-b", "shared")
        self.service.coding.submit(decision("b", "alice", "positive"))
        self.service.coding.submit(decision("b", "bob", "negative"))
        strata = self.service.agreement.strata(query())
        by_source = self.sources_by_id(strata)
        self.assertEqual(by_source["north-a"]["states"]["consensus"]["stories"], 1)
        self.assertEqual(by_source["north-b"]["states"]["disputed"]["stories"], 1)
        region = strata["regions"][0]
        self.assertEqual(region["stories"], 1)
        self.assertEqual(region["stories_in_multiple_states"], 1)
        report = self.report()
        self.assertEqual(report["story_count"], 1)
        self.assertEqual(report["stories"][0]["observation_count"], 1)

    def test_adjudication_never_counts_as_pairwise_consensus(self):
        self.add("a")
        self.service.coding.submit(decision("a", "alice", "positive"))
        self.service.coding.submit(decision("a", "bob", "negative"))
        self.service.coding.adjudicate(ruling("a"))
        strata = self.service.agreement.strata(query())
        source = self.sources_by_id(strata)["north-a"]
        self.assertEqual(source["states"]["consensus"]["observations"], 0)
        self.assertEqual(source["states"]["adjudicated"]["observations"], 1)
        self.assertEqual(source["coded"]["observations"], 1)
        self.assertEqual(source["pairwise_agreement"], 0.0)

    def test_correction_keeps_old_revision_opinions_on_old_manuscript(self):
        self.accepted("a")
        changed = self.service.observations.describe("local-fixture", "a")["data"] | {"story_key": "revised"}
        self.service.observations.ingest(changed, expected_revision=1)
        strata = self.service.agreement.strata(query())
        source = self.sources_by_id(strata)["north-a"]
        self.assertEqual(source["states"]["consensus"]["observations"], 0)
        self.assertEqual(source["states"]["uncoded"]["observations"], 1)
        impact = self.service.agreement.decision_impact("a-alice", query())
        self.assertFalse(impact["observation"]["is_head"])
        self.assertEqual(impact["observation"]["head_revision"], 2)
        self.assertEqual(impact["revision_coding_status"], "consensus")
        self.assertEqual(impact["head"]["category"], "uncoded")

    def test_incomplete_collection_shown_beside_observed_agreement(self):
        self.accepted("a", "north-a", "a-story")
        self.add("b", "north-b", "b-story")
        self.service.frames.open("frame-b", "north-b", query()["start"], query()["end"], 2)
        strata = self.service.agreement.strata(query())
        by_source = self.sources_by_id(strata)
        north_b = by_source["north-b"]
        self.assertTrue(north_b["coverage"]["declared"])
        self.assertFalse(north_b["coverage"]["complete"])
        self.assertEqual(north_b["coverage"]["problems"], [{"frame_id": "frame-b", "reason": "open"}])
        self.assertEqual(north_b["states"]["uncoded"]["observations"], 1)
        self.assertIsNone(north_b["pairwise_agreement"])
        north_a = by_source["north-a"]
        self.assertEqual(north_a["pairwise_agreement"], 1.0)
        self.assertFalse(north_a["coverage"]["declared"])

    def test_disputed_topic_is_undetermined_only_when_reviewers_split(self):
        self.add("same-topic")
        self.service.coding.submit(decision("same-topic", "alice", "positive"))
        self.service.coding.submit(decision("same-topic", "bob", "negative"))
        self.add("split-topic")
        self.service.coding.submit(decision("split-topic", "alice", "positive"))
        self.service.coding.submit(decision("split-topic", "bob", "negative", topic="culture"))
        strata = self.service.agreement.strata(query())
        topics = {row["topic"]: row for row in strata["topics"]}
        self.assertEqual(topics["technology"]["states"]["disputed"]["observations"], 1)
        self.assertEqual(topics["undetermined"]["states"]["disputed"]["observations"], 1)


class ContrastTests(ServiceCase):
    def seed_regions(self):
        self.add("n1", "north-a", "n1")
        self.code("n1", "positive")
        self.add("n2", "north-a", "n2")
        self.code("n2", "positive")
        self.add("n3", "north-b", "n3")
        self.code("n3", "negative")
        self.add("s1", "south-a", "s1")
        self.code("s1", "negative")
        self.add("s2", "south-a", "s2")
        self.code("s2", "negative")

    def test_leave_one_out_shows_source_structure_sensitivity(self):
        self.seed_regions()
        result = self.service.agreement.contrast(both_regions(), "North", "South")
        observed = result["scenarios"]["dispute_handling"]["observed"]["contrast"]
        self.assertEqual(observed["left"], {"story_count": 3, "stance_shares":
                                            {"negative": 0.333333, "positive": 0.666667}})
        self.assertEqual(observed["right"]["stance_shares"], {"negative": 1.0})
        self.assertEqual(observed["differences"], {"negative": 0.666667, "positive": -0.666667})
        leave = {row["source_id"]: row for row in result["scenarios"]["source_leave_out"]}
        self.assertEqual(set(leave), {"north-a", "north-b", "south-a"})
        self.assertEqual(leave["north-a"]["observations_removed"], 2)
        self.assertEqual(leave["north-a"]["stories_removed"], 2)
        self.assertEqual(leave["north-a"]["shifts"], {"negative": -0.666667, "positive": 0.666667})
        self.assertEqual(leave["north-b"]["shifts"], {"negative": 0.333333, "positive": -0.333333})
        self.assertIsNone(leave["south-a"]["contrast"]["differences"])
        self.assertIsNone(leave["south-a"]["shifts"])
        regions = {row["region"] for row in result["agreement"]["regions"]}
        self.assertEqual(regions, {"North", "South"})

    def test_dispute_handling_variants_keep_pending_visible(self):
        self.seed_regions()
        self.add("n4", "north-a", "n4")
        self.service.coding.submit(decision("n4", "alice", "positive"))
        self.service.coding.submit(decision("n4", "bob", "negative"))
        result = self.service.agreement.contrast(both_regions(), "North", "South")
        handling = result["scenarios"]["dispute_handling"]
        self.assertEqual(handling["pending"]["left"], {"disputed_observations": 1, "disputed_stories": 1})
        self.assertEqual(handling["pending"]["right"], {"disputed_observations": 0, "disputed_stories": 0})
        self.assertEqual(handling["observed"]["contrast"]["left"]["story_count"], 3)
        self.assertEqual(handling["pairwise_only"]["contrast"]["left"]["story_count"], 3)
        self.service.coding.adjudicate(ruling("n4", stance="positive"))
        result = self.service.agreement.contrast(both_regions(), "North", "South")
        handling = result["scenarios"]["dispute_handling"]
        self.assertEqual(handling["observed"]["contrast"]["left"]["stance_shares"],
                         {"negative": 0.25, "positive": 0.75})
        self.assertEqual(handling["pairwise_only"]["contrast"]["left"]["story_count"], 3)
        self.assertEqual(handling["pairwise_only"]["observations_set_aside"], {"adjudicated": 1})
        self.assertEqual(handling["pairwise_only"]["shifts"],
                         {"negative": -0.083333, "positive": 0.083333})
        self.assertEqual(handling["pending"]["left"], {"disputed_observations": 0, "disputed_stories": 0})


class DecisionImpactTests(ServiceCase):
    def test_impact_traces_strata_stories_and_frozen_releases(self):
        self.accepted("a", "north-a", "shared")
        preview = self.service.releases.preview(query())
        self.service.releases.publish("november", query(), preview["token"])
        impact = self.service.agreement.decision_impact("a-alice", query())
        self.assertEqual(impact["head"]["category"], "eligible")
        self.assertEqual(impact["stratum"], {"source_id": "north-a", "region": "North",
                                             "topic": "technology"})
        self.assertEqual([story["story_key"] for story in impact["stories"]], ["shared"])
        self.assertEqual(impact["releases"], [{"release_id": "november", "frozen_revision": 1,
                                               "frozen_category": "eligible"}])
        self.assertTrue(impact["in_current_export"])
        changed = self.service.observations.describe("local-fixture", "a")["data"] | {"story_key": "revised"}
        self.service.observations.ingest(changed, expected_revision=1)
        impact = self.service.agreement.decision_impact("a-alice", query())
        self.assertEqual(impact["head"]["category"], "uncoded")
        self.assertEqual(impact["stories"], [])
        self.assertEqual(impact["releases"][0]["frozen_category"], "eligible")
        self.assertEqual(self.service.releases.describe("november")["report"]["story_count"], 1)


class ChangeCauseTests(ServiceCase):
    def test_simultaneous_changes_split_into_composition_and_coding(self):
        self.accepted("keep", "north-a", "keep")
        self.accepted("shared", "north-a", "shared")
        self.add("pending", "north-b", "pending")
        preview = self.service.releases.preview(query())
        self.assertTrue(preview["quality"]["passed"])
        self.service.releases.publish("november", query(), preview["token"])
        self.accepted("fresh", "north-b", "fresh")
        changed = self.service.observations.describe("local-fixture", "keep")["data"] | {"story_key": "keep-v2"}
        self.service.observations.ingest(changed, expected_revision=1)
        self.code("pending", "negative")
        self.service.coding.submit(decision("shared", "carol", "negative"))
        self.accepted("shared-b", "north-b", "shared")
        causes = self.service.agreement.change_causes("november")
        self.assertFalse(causes["same_result"])
        self.assertEqual(causes["summary"], {"composition_only": 2, "coding_only": 1,
                                             "composition_and_coding": 1})
        by_story = {story["key"]["story_key"]: story for story in causes["changed_stories"]}
        self.assertEqual(by_story["keep"]["causes"], ["composition"])
        self.assertEqual(by_story["fresh"]["causes"], ["composition"])
        self.assertEqual(by_story["pending"]["causes"], ["coding"])
        self.assertEqual(by_story["shared"]["causes"], ["coding", "composition"])
        self.assertEqual(causes["event_counts"]["composition"],
                         {"observation_corrected": 1, "observation_ingested": 2})
        self.assertEqual(causes["event_counts"]["coding"], {"coding_submitted": 7})
        details = {(row["origin"], row["external_id"]): (row["cause"], row["detail"])
                   for row in causes["observation_causes"]}
        self.assertEqual(details[("local-fixture", "keep")], ("composition", "corrected"))
        self.assertEqual(details[("local-fixture", "pending")], ("coding", "uncoded_to_eligible"))
        self.assertEqual(details[("local-fixture", "shared")], ("coding", "eligible_to_disputed"))
        self.assertEqual(details[("local-fixture", "fresh")], ("composition", "ingested"))
        north = {row["region"]: row for row in causes["regions"]}["North"]
        self.assertEqual(north["before"], {"story_count": 2, "stance_shares": {"positive": 1.0}})
        self.assertEqual(north["after"]["story_count"], 3)
        self.assertEqual(north["after"]["stance_shares"], {"negative": 0.333333, "positive": 0.666667})

    def test_quiet_release_reports_no_causes(self):
        self.accepted("a")
        preview = self.service.releases.preview(query())
        self.service.releases.publish("november", query(), preview["token"])
        causes = self.service.agreement.change_causes("november")
        self.assertTrue(causes["same_result"])
        self.assertEqual(causes["observation_causes"], [])
        self.assertEqual(causes["changed_stories"], [])
        self.assertEqual(causes["summary"], {"composition_only": 0, "coding_only": 0,
                                             "composition_and_coding": 0})
        self.assertEqual(causes["event_counts"]["ignored"], {"report_published": 1})


class AgreementApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = str(Path(self.temp.name) / "api.sqlite3")
        self.app = create_app(path)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.state.service.close()
        self.temp.cleanup()

    def test_agreement_endpoints_over_http(self):
        self.client.post("/sources", json=source("a", "North", "Outlet A"))
        self.client.post("/sources", json=source("b", "South", "Outlet B"))
        self.client.post("/observations", json=observation("n1", "a", "n1"))
        self.client.post("/observations", json=observation("s1", "b", "s1"))
        for external_id in ("n1", "s1"):
            for reviewer in ("alice", "bob"):
                stance = "positive" if external_id == "n1" else "negative"
                self.client.post("/decisions", json=decision(external_id, reviewer, stance))
        both = query() | {"regions": ["North", "South"]}
        strata = self.client.post("/reports/agreement", json=both)
        self.assertEqual(strata.status_code, 200)
        self.assertEqual({row["source_id"] for row in strata.json()["sources"]}, {"a", "b"})
        contrast = self.client.post("/reports/agreement-contrast",
                                    json={"query": both, "left_region": "North", "right_region": "South"})
        self.assertEqual(contrast.status_code, 200)
        self.assertEqual(contrast.json()["scenarios"]["dispute_handling"]["observed"]
                         ["contrast"]["differences"], {"negative": 1.0, "positive": -1.0})
        impact = self.client.post("/reports/decision-impact",
                                  json={"decision_id": "n1-alice", "query": both})
        self.assertEqual(impact.status_code, 200)
        self.assertEqual(impact.json()["head"]["category"], "eligible")
        missing = self.client.post("/reports/decision-impact",
                                   json={"decision_id": "unknown", "query": both})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"], "decision_missing")
        preview = self.client.post("/reports/preview", json=both).json()
        published = self.client.post("/releases", json={"release_id": "november",
                                                        "query": both, "token": preview["token"]})
        self.assertEqual(published.status_code, 200)
        causes = self.client.get("/releases/november/change-causes")
        self.assertEqual(causes.status_code, 200)
        self.assertTrue(causes.json()["same_result"])


if __name__ == "__main__":
    unittest.main()
