"""Versioned model proposals, validated graph effects, and recurring reassessment."""
from collections import Counter
from copy import deepcopy
import json
import logging
from uuid import uuid4

from . import outcomes


class OutcomeWorld:
    def _init_outcomes(self):
        self.outcome_mode = "rules"  # API/runner explicitly opt into model authority.
        self.world_version = 0
        self.run_id = uuid4().hex
        self.outcome_events = []
        self._assessed = {}
        self._credited_tick = {}
        self._eligible_nodes = set()
        self.db._conn.execute("""CREATE TABLE IF NOT EXISTS world_outcomes (
            id INTEGER PRIMARY KEY, proposal_id TEXT UNIQUE NOT NULL,
            incident_id INTEGER NOT NULL, tick INTEGER NOT NULL, accepted INTEGER NOT NULL,
            event_json TEXT NOT NULL)""")
        self.db._conn.commit()

    def outcome_evidence(self, case_id):
        node = self.case_nodes[case_id]
        incident = self.state["active_incidents"][node]
        policy = self.scenario["nodes"][node]
        observed = self.evaluate_response(node)
        units = [{"unit_id":key, "resource_type":u["resource_type"], "status":u["status"],
                  "lat":u["lat"], "lon":u["lon"], "home_hub":u["home_hub"],
                  "remaining_busy_minutes":u["total_depletion_ticks_remaining"],
                  "route_blocked":u.get("route_blocked",False)}
                 for key,u in self.state["resource_db"].items() if u["assigned_cam"] == node]
        committed = Counter(u["resource_type"] for u in units
                            if u["status"] in ("DEPLETED_EN_ROUTE", "DEPLETED_ON_SCENE"))
        source_id = incident["incident_id"]
        followups = [deepcopy(r) for r in self.follow_up_reports if r["report"]["incident_id"] == source_id]
        eligible = int(node in self._eligible_nodes and observed["capability_coverage"]
                       and self._credited_tick.get(node) != self.state["tick"])
        after_work = observed["effective_work_ticks"] + eligible
        return {
            "proposal_id":f"outcome:{self.run_id}:{case_id}:{self.world_version}:{self.state['tick']}",
            "incident_id":case_id, "node_id":node, "world_version":self.world_version,
            "tick":self.state["tick"], "observed_at":self.timestamp(),
            "caller_report":deepcopy(incident["source_report"]), "follow_up_reports":followups,
            "observed":{**observed, "severity":incident["severity"], "status":incident["status"],
                        "road_blocked":incident["road_blocked"], "routing_mode":self.state["routing"]["mode"]},
            "units":units,
            "eligible_work_credit":eligible,
            "work_projection_if_credit_awarded":{
                "work_before":observed["effective_work_ticks"], "credit":eligible, "work_after":after_work,
                "containment_threshold_met":after_work >= policy["containment_work_ticks"],
                "resolution_threshold_met":after_work >= policy["resolution_work_ticks"],
                "label":"Projection, not yet applied; evaluate the outcome AFTER your proposed credit."},
            "missing_committed_types":{t:max(0,n-committed[t]) for t,n in policy["required_resource_types"].items()
                                       if n > committed[t]},
            "game_policy":{k:deepcopy(policy[k]) for k in
                ("required_resource_types", "containment_work_ticks", "resolution_work_ticks")},
            "media":"No fresh post-dispatch image supplied; do not infer visual outcomes.",
        }

    def _validate_proposal(self, proposal, evidence):
        if self.db._conn.execute("SELECT 1 FROM world_outcomes WHERE proposal_id=?", (proposal.proposal_id,)).fetchone():
            raise ValueError("Duplicate proposal_id; effect already recorded")
        for key in ("proposal_id", "incident_id", "node_id", "world_version", "tick"):
            if getattr(proposal,key) != evidence[key]:
                raise ValueError("Proposal does not match observed " + key)
        if evidence["world_version"] != self.world_version or evidence["tick"] != self.state["tick"]:
            raise ValueError("Stale world evidence")
        if evidence != self.outcome_evidence(proposal.incident_id):
            raise ValueError("Evidence no longer matches current world")
        if evidence["observed"]["status"] == "RESOLVED":
            raise ValueError("Incident already resolved")
        if proposal.work_credit > evidence["eligible_work_credit"]:
            raise ValueError("No complete working tick with required on-scene typed resources")
        expected = outcomes.mock_proposal(evidence).outcome
        if proposal.work_credit == 0 and evidence["eligible_work_credit"]:
            expected = "no_change"  # The model may defer when evidence is insufficient.
        if proposal.outcome != expected:
            raise ValueError("Outcome contradicts work, capability or game thresholds")

    def apply_outcome(self, proposal, evidence, executed_by):
        """Only this method turns a proposal into world effects; apply once, atomically."""
        proposal = outcomes.OutcomeProposal.model_validate(proposal.model_dump())
        self._validate_proposal(proposal,evidence)
        node = proposal.node_id
        incident = self.state["active_incidents"][node]
        before = (deepcopy(self.state), deepcopy(self._work), set(self._contained), dict(self._credited_tick))
        try:
            if proposal.work_credit:
                self._record_response(incident)
                self._work[incident["incident_id"]] += proposal.work_credit
                self._credited_tick[node] = self.state["tick"]
                incident["severity"] = max(1,self.scenario["nodes"][node]["severity"]-self._work[incident["incident_id"]])
            if proposal.outcome in ("contained", "resolved"):
                self._contained.add(incident["incident_id"])
            if proposal.outcome == "resolved":
                incident["status"] = "RESOLVED"
                self.state["effectiveness_metrics"]["incidents_resolved"] += 1
                if incident["road_blocked"]:
                    self.set_camera_road_closed(node,False)
                    incident["road_blocked"] = False
            event = {"accepted":True, "executed_by":executed_by, "proposal":proposal.model_dump(),
                     "evidence":evidence, "after":{"status":incident["status"], "severity":incident["severity"],
                                                  **self.evaluate_response(node)}}
            with self.db.tx() as conn:
                conn.execute("INSERT INTO world_outcomes (proposal_id,incident_id,tick,accepted,event_json) VALUES (?,?,?,?,?)",
                             (proposal.proposal_id,proposal.incident_id,proposal.tick,1,json.dumps(event)))
        except Exception:
            self.state,self._work,self._contained,self._credited_tick = before
            raise
        self.world_version += 1
        self.outcome_events.append(event)
        self.response_events.append({"tick":proposal.tick,"incident_id":incident["incident_id"],
            "success_metrics":self.evaluate_response(node),"world_status":incident["status"],
            "outcome_proposal_id":proposal.proposal_id})
        return event

    def _reject_outcome(self, evidence, error, executed_by, proposal=None):
        event = {"accepted":False, "executed_by":executed_by, "evidence":evidence, "error":str(error),
                 "proposal":proposal.model_dump() if proposal is not None else None}
        with self.db.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO world_outcomes (proposal_id,incident_id,tick,accepted,event_json) VALUES (?,?,?,?,?)",
                         (evidence["proposal_id"],evidence["incident_id"],evidence["tick"],0,json.dumps(event)))
        self.outcome_events.append(event)
        return event

    async def reassess(self, orchestrator, on_event=None):
        """Changed attendance/reports immediately; working ticks each minute; transit every 5 min."""
        if self.inflight:
            from .world import WorldConflict
            raise WorldConflict("A model decision window is already open")
        self.inflight += 1
        events = []
        try:
            for case_id,node in list(self.case_nodes.items()):
                if case_id not in self.responses or self.state["active_incidents"][node]["status"] == "RESOLVED":
                    continue
                evidence = self.outcome_evidence(case_id)
                signature = json.dumps([[(u["unit_id"],u["status"],u["route_blocked"]) for u in evidence["units"]],
                                        evidence["follow_up_reports"]],sort_keys=True)
                last = self._assessed.get(case_id)
                working = any(u["status"] == "DEPLETED_ON_SCENE" for u in evidence["units"])
                if last and last[1] == signature and (last[0] == evidence["tick"] or
                        (not working and evidence["tick"]-last[0] < 5)):
                    continue
                executed = "qwen" if orchestrator.settings.live else "mock"
                proposal = None
                try:
                    proposal,executed = await outcomes.propose(orchestrator.settings,evidence)
                    event = self.apply_outcome(proposal,evidence,executed)
                except ValueError as error:
                    event = self._reject_outcome(evidence,error,executed,proposal)
                self._assessed[case_id] = (evidence["tick"],signature)
                self.db.add_log(case_id,"world_outcome", ("Applied: " + event["proposal"]["outcome"] + ". " + event["proposal"]["reason"])
                                if event["accepted"] else "Rejected outcome: " + event["error"])
                if on_event:
                    try:
                        await on_event(case_id,"world_outcome",event)
                    except Exception:
                        logging.getLogger(__name__).exception("Outcome listener failed")
                events.append(event)
            self._sync_outcomes()
        finally:
            self.inflight -= 1
        return events

    async def advance(self, orchestrator, on_event=None):
        if self.outcome_mode == "rules":
            return self.tick()
        if self.inflight:
            from .world import WorldConflict
            raise WorldConflict("Finish model decision windows before advancing the clock")
        self._eligible_nodes = {node for node,i in self.state["active_incidents"].items()
                                if i["status"] != "RESOLVED" and self.evaluate_response(node)["capability_coverage"]}
        self._begin_tick()
        self.world_version += 1
        await self.reassess(orchestrator,on_event)
        self._finish_tick()
        self._sync_outcomes()
        self.last_released = self.db.release_due()
        return self.state
