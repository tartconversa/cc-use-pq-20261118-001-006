"""按来源与议题分层的编码一致性、地区结论情景与变化归因。

一致性只描述已观察到的证据：裁定后的案件永远单独成层，不回算成双人一致；
采集覆盖不足与编码稳定性分开陈列，互不填补；变化归因只给可核对的分账，
不合成单一分数。
"""
from collections import Counter, defaultdict
from .contracts import digest, report_query, text
from .errors import require
from .storage import decode

STATES = ("consensus", "disputed", "adjudicated", "uncoded", "retracted")
CODED_STATES = ("consensus", "disputed", "adjudicated")
COMPOSITION_EVENTS = {"observation_ingested", "observation_corrected", "observation_retracted",
                      "source_registered", "source_deactivated", "frame_opened", "frame_closed"}
CODING_EVENTS = {"coding_submitted", "case_adjudicated"}
IGNORED_EVENTS = {"batch_opened", "report_published"}


def _new_bucket():
    return {"observations": 0,
            "states": {state: {"observations": 0, "stories": set()} for state in STATES},
            "stories": set()}


class AgreementAnalyzer:
    def __init__(self, store, observations, sources, coding, analytics, frames, comparisons, releases):
        self.store = store
        self.observations = observations
        self.sources = sources
        self.coding = coding
        self.analytics = analytics
        self.frames = frames
        self.comparisons = comparisons
        self.releases = releases

    @staticmethod
    def _state_of(row):
        if row["item"]["status"] == "retracted":
            return "retracted"
        status = row["state"]["status"]
        return "uncoded" if status == "awaiting_review" else status

    @staticmethod
    def _topic_of(row):
        coding = row["state"].get("coding")
        if coding:
            return coding["topic"]
        decisions = row["state"]["decisions"]
        if row["state"]["status"] == "disputed" and len({row["topic"] for row in decisions}) == 1:
            return decisions[0]["topic"]
        return None

    def _accumulate(self, rows):
        buckets = {"sources": defaultdict(_new_bucket), "topics": defaultdict(_new_bucket),
                   "cells": defaultdict(_new_bucket), "regions": defaultdict(_new_bucket)}
        for row in rows:
            data = row["item"]["data"]
            region = row["source"]["region"]
            state = self._state_of(row)
            topic = self._topic_of(row) or "undetermined"
            story = (region, data["story_key"])
            targets = (buckets["sources"][data["source_id"]], buckets["topics"][topic],
                       buckets["cells"][(data["source_id"], topic)], buckets["regions"][region])
            for bucket in targets:
                bucket["observations"] += 1
                bucket["states"][state]["observations"] += 1
                bucket["states"][state]["stories"].add(story)
                bucket["stories"].add(story)
        return buckets

    @staticmethod
    def _finalize(bucket):
        states = {}
        for state in STATES:
            states[state] = {"observations": bucket["states"][state]["observations"],
                             "stories": len(bucket["states"][state]["stories"])}
        total = sum(entry["observations"] for entry in states.values())
        require(total == bucket["observations"], "strata_mismatch",
                "stratum states do not reconcile to observations")
        coded_stories = set()
        for state in CODED_STATES:
            coded_stories |= bucket["states"][state]["stories"]
        coded_observations = sum(states[state]["observations"] for state in CODED_STATES)
        agreement = None
        if coded_observations:
            agreement = round(states["consensus"]["observations"] / coded_observations, 6)
        multi_state = sum(1 for story in bucket["stories"]
                          if sum(story in bucket["states"][state]["stories"] for state in STATES) > 1)
        return {"observations": bucket["observations"], "states": states,
                "coded": {"observations": coded_observations, "stories": len(coded_stories)},
                "stories": len(bucket["stories"]), "stories_in_multiple_states": multi_state,
                "pairwise_agreement": agreement}

    def _coverage_by_source(self, spec):
        grouped = defaultdict(lambda: {"frames": [], "problems": []})
        for frame in self.frames.coverage(spec):
            entry = grouped[frame["source_id"]]
            entry["frames"].append({"frame_id": frame["frame_id"], "state": frame["state"],
                                    "expected_count": frame["expected_count"],
                                    "current_count": frame["current_count"], "drifted": frame["drifted"]})
            if frame["state"] != "closed":
                entry["problems"].append({"frame_id": frame["frame_id"], "reason": "open"})
            elif frame["drifted"]:
                entry["problems"].append({"frame_id": frame["frame_id"], "reason": "drifted"})
        return grouped

    def strata(self, query):
        spec = report_query(query)
        rows = self.analytics._rows(spec)
        buckets = self._accumulate(rows)
        coverage = self._coverage_by_source(spec)
        sources = []
        known = [row for row in self.sources.list() if not spec["regions"] or row["region"] in spec["regions"]]
        for source in sorted(known, key=lambda row: row["source_id"]):
            bucket = buckets["sources"].get(source["source_id"], _new_bucket())
            entry = self._finalize(bucket)
            declared = coverage.get(source["source_id"], {"frames": [], "problems": []})
            entry.update({"source_id": source["source_id"], "outlet": source["outlet"],
                          "region": source["region"],
                          "coverage": {"declared": bool(declared["frames"]),
                                       "complete": bool(declared["frames"]) and not declared["problems"],
                                       "frames": declared["frames"], "problems": declared["problems"]}})
            sources.append(entry)
        topics = [dict(self._finalize(bucket), topic=topic)
                  for topic, bucket in sorted(buckets["topics"].items())]
        cells = [dict(self._finalize(bucket), source_id=source_id, topic=topic)
                 for (source_id, topic), bucket in sorted(buckets["cells"].items())]
        region_names = set(buckets["regions"]) | set(spec["regions"])
        regions = [dict(self._finalize(buckets["regions"].get(region, _new_bucket())), region=region)
                   for region in sorted(region_names)]
        total = len(rows)
        require(sum(entry["observations"] for entry in sources) == total, "strata_mismatch",
                "source strata do not reconcile to window")
        require(sum(entry["observations"] for entry in topics) == total, "strata_mismatch",
                "topic strata do not reconcile to window")
        require(sum(entry["observations"] for entry in cells) == total, "strata_mismatch",
                "source-topic cells do not reconcile to window")
        require(sum(entry["observations"] for entry in regions) == total, "strata_mismatch",
                "region strata do not reconcile to window")
        return {"query": spec, "query_hash": digest(spec), "event_head": self.store.head(),
                "observations_in_window": total, "sources": sources, "topics": topics,
                "cells": cells, "regions": regions}

    @staticmethod
    def _contrast(summary, left_region, right_region):
        regions = {row["region"]: row for row in summary["regions"]}
        sides = {}
        counts = {}
        for name, region in (("left", left_region), ("right", right_region)):
            row = regions.get(region)
            total = row["story_count"] if row else 0
            counts[name] = row["stance_counts"] if row else {}
            sides[name] = {"story_count": total,
                           "stance_shares": {stance: round(count / total, 6)
                                             for stance, count in sorted(counts[name].items())}
                           if total else {}}
        differences = None
        if sides["left"]["story_count"] and sides["right"]["story_count"]:
            stances = sorted(set(counts["left"]) | set(counts["right"]))
            differences = {stance: round(counts["right"].get(stance, 0) / sides["right"]["story_count"]
                                         - counts["left"].get(stance, 0) / sides["left"]["story_count"], 6)
                           for stance in stances}
        return {"left": sides["left"], "right": sides["right"], "differences": differences}

    @staticmethod
    def _shifts(baseline, scenario):
        if baseline is None or scenario is None:
            return None
        return {stance: round(scenario.get(stance, 0) - baseline.get(stance, 0), 6)
                for stance in sorted(set(baseline) | set(scenario))}

    def contrast(self, query, left_region, right_region):
        spec = report_query(query)
        comparison = self.comparisons.regions(spec, left_region, right_region)
        rows = self.analytics._rows(spec)
        baseline = self._contrast(self.analytics._summarize(rows, spec), left_region, right_region)
        require(baseline["differences"] == {stance: row["difference"]
                                            for stance, row in comparison["stances"].items()},
                "contrast_mismatch", "baseline contrast does not match region comparison")
        strata = self.strata(spec)
        agreement = {"regions": [row for row in strata["regions"]
                                 if row["region"] in {left_region, right_region}],
                     "sources": [row for row in strata["sources"]
                                 if row["region"] in {left_region, right_region}]}
        rollups = {row["region"]: row for row in agreement["regions"]}
        leave_out = []
        for source in agreement["sources"]:
            if not source["observations"]:
                continue
            kept = [row for row in rows if row["item"]["data"]["source_id"] != source["source_id"]]
            require(len(rows) - len(kept) == source["observations"], "contrast_mismatch",
                    "leave-one-out does not match source stratum")
            scenario = self._contrast(self.analytics._summarize(kept, spec), left_region, right_region)
            leave_out.append({"source_id": source["source_id"], "outlet": source["outlet"],
                              "region": source["region"],
                              "observations_removed": source["observations"],
                              "stories_removed": comparison["left"]["story_count"]
                              + comparison["right"]["story_count"]
                              - scenario["left"]["story_count"] - scenario["right"]["story_count"],
                              "contrast": scenario,
                              "shifts": self._shifts(baseline["differences"], scenario["differences"])})
        consensus_rows = [row for row in rows if self._state_of(row) == "consensus"]
        set_aside = Counter(self._state_of(row) for row in rows if self._state_of(row) != "consensus")
        pairwise = self._contrast(self.analytics._summarize(consensus_rows, spec),
                                  left_region, right_region)
        pending = {}
        for name, region in (("left", left_region), ("right", right_region)):
            rollup = rollups.get(region)
            disputed = rollup["states"]["disputed"] if rollup else {"observations": 0, "stories": 0}
            pending[name] = {"disputed_observations": disputed["observations"],
                             "disputed_stories": disputed["stories"]}
        return {"query_hash": comparison["query_hash"], "event_head": comparison["event_head"],
                "left_region": left_region, "right_region": right_region,
                "comparison": comparison, "agreement": agreement,
                "scenarios": {"source_leave_out": leave_out,
                              "dispute_handling": {
                                  "observed": {"contrast": {"left": baseline["left"],
                                                            "right": baseline["right"],
                                                            "differences": baseline["differences"]}},
                                  "pairwise_only": {"contrast": pairwise,
                                                    "shifts": self._shifts(baseline["differences"],
                                                                           pairwise["differences"]),
                                                    "observations_set_aside": dict(sorted(set_aside.items()))},
                                  "pending": pending}}}

    def decision_impact(self, decision_id, query):
        spec = report_query(query)
        decision = self.coding.describe(text(decision_id, "decision_id"))
        origin, external_id, revision = (decision["origin"], decision["external_id"],
                                         decision["revision"])
        item = self.observations.describe(origin, external_id, revision)
        head = self.observations.describe(origin, external_id)
        source = self.sources.describe(head["data"]["source_id"])
        state = self.coding.status(origin, external_id, revision)
        report = self.analytics.report(spec)
        rows = [row for row in self.analytics._rows(spec)
                if row["item"]["data"]["origin"] == origin
                and row["item"]["data"]["external_id"] == external_id]
        category = None
        head_state = None
        if rows:
            category = self.analytics._classify(rows[0], spec)
            head_state = self._state_of(rows[0])
        elif head["revision"] != revision:
            category = "superseded"
        elif not spec["start"] <= head["data"]["published_at"] < spec["end"]:
            category = "outside_window"
        else:
            category = "outside_region"
        stories = []
        if category == "eligible":
            stories = [story for story in report["stories"] if story["region"] == source["region"]
                       and story["story_key"] == head["data"]["story_key"]]
        releases = []
        for row in self.store.all("SELECT release_id,report_json FROM releases ORDER BY release_id"):
            frozen = decode(row["report_json"])
            for provenance in frozen["provenance"]:
                if (provenance["origin"], provenance["external_id"]) == (origin, external_id):
                    releases.append({"release_id": row["release_id"],
                                     "frozen_revision": provenance["revision"],
                                     "frozen_category": provenance["category"]})
        return {"decision": decision,
                "observation": {"origin": origin, "external_id": external_id, "revision": revision,
                                "is_head": item["is_head"], "status": item["status"],
                                "head_revision": head["revision"]},
                "revision_coding_status": state["status"],
                "head": {"category": category, "state": head_state},
                "stratum": {"source_id": source["source_id"], "region": source["region"],
                            "topic": (self._topic_of(rows[0]) or "undetermined") if rows else None},
                "stories": stories, "in_current_export": bool(rows), "releases": releases,
                "query_hash": report["query_hash"], "event_head": report["event_head"]}

    def change_causes(self, release_id):
        release_id = text(release_id, "release_id")
        frozen = self.releases.describe(release_id)
        old = frozen["report"]
        current = self.analytics.report(old["query"])
        events = [dict(row) for row in self.store.all(
            "SELECT * FROM events WHERE event_no>? ORDER BY event_no", (frozen["event_head"],))]
        event_counts = {"composition": Counter(), "coding": Counter(), "ignored": Counter()}
        for event in events:
            kind = event["kind"]
            if kind in COMPOSITION_EVENTS:
                event_counts["composition"][kind] += 1
            elif kind in CODING_EVENTS:
                event_counts["coding"][kind] += 1
            else:
                require(kind in IGNORED_EVENTS, "event_unclassified",
                        "event kind cannot be attributed", kind=kind)
                event_counts["ignored"][kind] += 1
        decision_index = {row["decision_id"]: (row["origin"], row["external_id"])
                          for row in self.store.all("SELECT decision_id,origin,external_id FROM decisions")}
        adjudication_index = {row["case_key"]: (row["origin"], row["external_id"])
                              for row in self.store.all("SELECT case_key,origin,external_id FROM adjudications")}
        frozen_prov = {(row["origin"], row["external_id"]): row for row in old["provenance"]}
        current_prov = {(row["origin"], row["external_id"]): row for row in current["provenance"]}
        observation_index = {f"{origin}/{external_id}": (origin, external_id)
                             for origin, external_id in set(frozen_prov) | set(current_prov)}
        events_by_observation = defaultdict(list)
        for event in events:
            if event["kind"] not in COMPOSITION_EVENTS | CODING_EVENTS:
                continue
            key = (decision_index.get(event["entity_key"])
                   or adjudication_index.get(event["entity_key"])
                   or observation_index.get(event["entity_key"]))
            if key is not None:
                events_by_observation[key].append(event)
        causes_by_key = {}
        observation_causes = []
        for key in sorted(set(frozen_prov) | set(current_prov)):
            before = frozen_prov.get(key)
            after = current_prov.get(key)
            if before and after and before["revision"] == after["revision"] \
                    and before["category"] == after["category"]:
                continue
            related = events_by_observation.get(key, [])
            kinds = {event["kind"] for event in related}
            cause = detail = None
            if before is None:
                require(kinds & {"observation_ingested", "observation_corrected"}, "cause_unexplained",
                        "new observation has no collection event", origin=key[0], external_id=key[1])
                cause = "composition"
                detail = "ingested" if "observation_ingested" in kinds else "corrected_into_window"
            elif after is None:
                require("observation_corrected" in kinds, "cause_unexplained",
                        "observation left window without correction", origin=key[0], external_id=key[1])
                cause, detail = "composition", "corrected_out_of_window"
            elif before["revision"] != after["revision"]:
                require("observation_corrected" in kinds, "cause_unexplained",
                        "revision changed without correction event", origin=key[0], external_id=key[1])
                cause, detail = "composition", "corrected"
            elif "observation_retracted" in kinds:
                cause, detail = "composition", "retracted"
            else:
                require(kinds & CODING_EVENTS, "cause_unexplained",
                        "category changed without coding event", origin=key[0], external_id=key[1])
                cause = "coding"
                detail = f'{before["category"]}_to_{after["category"]}'
            entry = {"origin": key[0], "external_id": key[1], "cause": cause, "detail": detail,
                     "frozen": before, "current": after,
                     "events": [event["event_no"] for event in related]}
            causes_by_key[key] = entry
            observation_causes.append(entry)
        frozen_members = defaultdict(set)
        for key, provenance in frozen_prov.items():
            item = self.observations.describe(key[0], key[1], provenance["revision"])
            source = self.sources.describe(item["data"]["source_id"])
            frozen_members[(source["region"], item["data"]["story_key"])].add(key)
        current_members = defaultdict(set)
        for row in self.analytics._rows(old["query"]):
            data = row["item"]["data"]
            current_members[(row["source"]["region"], data["story_key"])].add(
                (data["origin"], data["external_id"]))

        def story_id(story):
            return (story["region"], story["story_key"], story["topic"], story["audience"])

        before_stories = {story_id(story): story for story in old["stories"]}
        after_stories = {story_id(story): story for story in current["stories"]}
        changed_stories = []
        for key in sorted(set(before_stories) | set(after_stories)):
            before = before_stories.get(key)
            after = after_stories.get(key)
            if before == after:
                continue
            members = frozen_members.get((key[0], key[1]), set()) | current_members.get((key[0], key[1]), set())
            causes = sorted({causes_by_key[member]["cause"] for member in members
                             if member in causes_by_key})
            require(causes, "cause_unexplained", "changed story has no attributed observation",
                    region=key[0], story_key=key[1])
            changed_stories.append({"key": {"region": key[0], "story_key": key[1],
                                            "topic": key[2], "audience": key[3]},
                                    "change": "added" if before is None else "removed"
                                    if after is None else "revised",
                                    "causes": causes,
                                    "observations": [causes_by_key[member] for member in sorted(members)
                                                     if member in causes_by_key],
                                    "before": before, "after": after})
        summary = {"composition_only": 0, "coding_only": 0, "composition_and_coding": 0}
        for story in changed_stories:
            if story["causes"] == ["composition"]:
                summary["composition_only"] += 1
            elif story["causes"] == ["coding"]:
                summary["coding_only"] += 1
            else:
                summary["composition_and_coding"] += 1
        old_regions = {row["region"]: row for row in old["regions"]}
        current_regions = {row["region"]: row for row in current["regions"]}
        regions = []
        for region in sorted(set(old_regions) | set(current_regions)):
            before = old_regions.get(region, {})
            after = current_regions.get(region, {})
            regions.append({"region": region,
                            "before": {"story_count": before.get("story_count", 0),
                                       "stance_shares": before.get("stance_shares", {})},
                            "after": {"story_count": after.get("story_count", 0),
                                      "stance_shares": after.get("stance_shares", {})}})
        return {"release_id": release_id, "frozen_head": frozen["event_head"],
                "current_head": current["event_head"],
                "event_counts": {name: dict(sorted(counts.items()))
                                 for name, counts in event_counts.items()},
                "observation_causes": observation_causes, "changed_stories": changed_stories,
                "summary": summary, "regions": regions,
                "same_result": not changed_stories and not observation_causes}
