# Resource Allocator Skill (Sheffield POC)

Load `SKILL.md` as the fourth agent's instructions. Pass an input object conforming to `allocator_input_schema.json` containing the four event summaries, the same snapshot of inventory, and all three Nemotron proposal objects. Require an output object conforming to `allocator_output_schema.json`.

The allocator recommends quantities but does not dispatch. Validate output *in code* before using it: service/snapshot/event IDs must match; approved <= requested; per-type sum approved <= inventory available; remaining=available-approved; unmet=requested-approved; every reduction must be justified; and quantities/decisions must be consistent. JSON Schema alone cannot enforce cross-record sums.

Resource inventory can reuse `resource_inventory_example.json` from the earlier `nemotron_resource_skills` package. Specialist outputs can reuse the earlier `proposal_schema.json`.

Example: with 5 police patrol cars and two requests for 3 and 2, approve 3 and 2 unless a specific marginal request is unsupported. Do not reserve cars arbitrarily. With 3 and 3 requested, explicitly adjudicate the shortage; do not claim that a cut to 1 is an adequate response.

Simulation only. Not validated emergency dispatch guidance.
