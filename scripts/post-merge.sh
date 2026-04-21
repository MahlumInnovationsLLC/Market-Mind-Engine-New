#!/bin/bash
set -e
pnpm install --frozen-lockfile
uv sync --frozen
pnpm --filter db push
