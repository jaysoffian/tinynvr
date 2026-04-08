check:
	prek run --all-files

run:
	uv run uvicorn nvr.app:app --host 0.0.0.0 --port 8554 --reload

update:
	prek autoupdate
	uv sync --upgrade
