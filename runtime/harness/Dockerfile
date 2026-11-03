FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LANG=C.UTF-8
RUN groupadd --gid 10000 research && useradd --uid 10000 --gid research --no-create-home research \
    && mkdir -p /opt/voxquarry /inputs /workspace /state /run/voxquarry \
    && chown 10000:10000 /inputs /workspace /state /run/voxquarry
WORKDIR /workspace

FROM base AS analysis
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim fonts-wqy-zenhei=0.9.45-8 \
    && rm -rf /var/lib/apt/lists/*
COPY runtime/harness/requirements-analysis.txt /opt/voxquarry/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: --report /opt/voxquarry/python-install-report.json -r /opt/voxquarry/requirements.txt
COPY runtime/harness/requirements-runner.txt /opt/voxquarry/requirements-runner.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /opt/voxquarry/requirements-runner.txt
COPY runtime/harness/analysis_worker.py runtime/harness/report_render.py runtime/harness/native_service.py runtime/harness/native-service.mjs runtime/harness/native-service.patch.yml /opt/voxquarry/
# Local deterministic composition: import research_methods / python -m research_methods.
# Standard-library-only; does not install a second reporting stack or remote tool.
COPY agent/research_methods.py /usr/local/lib/python3.11/site-packages/research_methods.py
COPY agent/research_skills /opt/voxquarry/skills
USER 10000:10000
ENTRYPOINT ["python", "/opt/voxquarry/analysis_worker.py"]

FROM base AS runner
COPY runtime/harness/requirements-runner.txt /opt/voxquarry/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: --report /opt/voxquarry/python-install-report.json -r /opt/voxquarry/requirements.txt \
    && python -c "from deepseek_harness_runtime import resolve_bundled_launch_args; print(resolve_bundled_launch_args())"
COPY runtime/harness/runner.py runtime/harness/session_resume.py runtime/harness/session-resume.mjs runtime/harness/research-tools.mjs runtime/harness/native-providers.mjs runtime/harness/native-web.mjs runtime/harness/research.patch.yml /opt/voxquarry/
COPY agent/research_skills /opt/voxquarry/skills
USER 10000:10000
ENTRYPOINT ["python", "/opt/voxquarry/runner.py"]

FROM base AS relay
COPY runtime/harness/relay.py /opt/voxquarry/relay.py
USER 10000:10000
ENTRYPOINT ["python", "/opt/voxquarry/relay.py"]

FROM docker:28.5.1-cli@sha256:9190b0613792e658a7783cf14b2d5ace5941bb68ede7276922ea36ee457d76ad AS dockercli
FROM base AS manager
RUN apt-get update && apt-get install -y --no-install-recommends iptables \
    && rm -rf /var/lib/apt/lists/*
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY runtime/harness/manager.py runtime/harness/firewall.py /opt/voxquarry/
# This trusted process alone owns the dedicated VM socket. Model containers
# neither inherit this image nor mount the socket or manager credentials.
ENTRYPOINT ["python", "/opt/voxquarry/manager.py"]
