# Sheffield Nemotron resource proposal skills (simulation POC)

Three separate Nemotrons independently assess **all** event clusters and propose quantities of their own service vehicles. Their proposals are unconstrained desired demand; availability is supplied for shortage awareness only. A separate senior arbiter decides allocations.

## Files
- `resource_inventory_example.json`: sample simulation availability snapshot; edit counts/types to reflect your simulator.
- `agent_input_schema.json`: per-Nemotron input contract.
- `proposal_schema.json`: per-Nemotron output contract.
- `<service>/SKILL.md`: system prompt for that Nemotron.
- `<service>/agent_input_example.json`: four-event example for that service.
- `police/proposal_output_example.json`: illustrative output.

## Integration
1. Run `incident_cluster_handoff.py --threshold 0.85 --output event_handoff.json`.
2. Load the handoff's `events` array and a consistent resource snapshot. Build one input object per service using `agent_input_schema.json`.
3. Pass each input as USER data and the matching `SKILL.md` as the SYSTEM prompt to a separate Nemotron instance.
4. Parse and validate each output against `proposal_schema.json` (e.g. `jsonschema.validate`). Additionally check service matches the invoked model, event IDs match exactly once, ranks are unique 1..N, resource types are allowed, totals are accurate, and evidence quotes are verbatim substrings of input summaries. JSON Schema alone cannot enforce these cross-field checks.
5. Send all three proposals, the inventory snapshot and original evidence to the senior arbiter. Arbiter chooses feasible allocations; the simulator performs atomic resource assignment and reports shortages.

## Note
The example quantities and severity scale are fictional heuristics for simulation only, not UK emergency dispatch standards. No automatic real-world dispatch. Do not pass `event_id` ground-truth labels to agents.
