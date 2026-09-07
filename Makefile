.PHONY: smoke-cli smoke-mcp

# Creates synthetic durable spend in a disposable staging project; requires SOLWYN_JWT.
smoke-cli:
	@uv run --locked python scripts/budget-plane-cli-smoke.py $(SMOKE_ARGS)

smoke-mcp:
	@uv run --locked python scripts/budget-plane-mcp-smoke.py $(SMOKE_ARGS)
