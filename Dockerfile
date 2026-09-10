# One image, two services. app.scheduler and app.workers.publisher differ only
# in their start command, and they must not drift in anything else: the
# publisher resolves brands and Page ids through the same app.domain.brand the
# scheduler drafts with, so a divergence between two images would be a
# divergence in what a brand *is*.
#
# Chromium is the reason this is a Dockerfile rather than a buildpack. The post
# graphic is a screenshot of app/render/templates/post.html, which needs a real
# browser plus its system libraries -- neither of which any Python buildpack
# installs, and whose absence surfaces as a failed render on every post rather
# than as a build error anyone would notice.
# Tag tracks the playwright version uv.lock pins (1.62.0) -- the base image
# ships the Chromium build that release expects, so they must move together.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

# uv, pinned, matching what wrote uv.lock locally. Installing with the same tool
# is what makes `--frozen` below mean anything.
COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /uvx /usr/local/bin/

# Dependencies before source, so editing a module does not reinstall the world.
COPY pyproject.toml uv.lock ./

# --frozen: install exactly what uv.lock pins and fail if it disagrees with
# pyproject.toml. Without it an unpinned dependency -- and pyproject.toml pins
# almost none of them -- resolves to whatever was published this morning, which
# for langchain and langgraph is a real risk rather than a theoretical one.
#
# --no-dev: pytest, ruff, mypy and import-linter have no business in a
# production image.
RUN uv sync --frozen --no-dev

COPY . .

# `[tool.uv] package = false` -- the project is never installed, `app` is
# imported from the working directory. So the interpreter has to be the one uv
# built, and it has to run from here.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

# Chromium is already in this base image, but the *version* pinned by whichever
# playwright uv.lock resolved may not be. Cheap when it matches, and the
# alternative is a browser that fails to launch at runtime.
RUN playwright install chromium

# Nothing sensible to default to: a container started without a command should
# fail loudly rather than silently become one of the two services. Railway sets
# the real command per service -- see DEPLOY.md.
CMD ["python", "-c", "import sys; sys.exit('Set a start command: see DEPLOY.md')"]
