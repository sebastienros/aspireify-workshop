# Aspireify Your Stack

Materials for the Aspireify Your Stack workshop, first given at NDC Oslo 2026.

## Workshop materials

- [Agenda](content/agenda.md)
- [Day 2 content and exercises](content/day-2.md)
- [Demo application](demo/README.md)

## Prerequisites

Install and verify the following before the workshop:

- [Git](https://git-scm.com/downloads)
- [.NET 10 SDK](https://dotnet.microsoft.com/download/dotnet/10.0)
- [Node.js 24 LTS](https://nodejs.org/en/download), including npm
- [Podman Desktop](https://podman-desktop.io/downloads) or [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- A code editor such as [Visual Studio Code](https://code.visualstudio.com/)
- A modern web browser

Make sure you can install software, trust a local development certificate, and download packages and container images from GitHub, NuGet, npm, and public container registries.

### Aspire CLI

We will install the [Aspire CLI](https://aspire.dev/get-started/install-cli/) together during the workshop and walk through what the installation adds to your environment. You are welcome to install it ahead of time, but it is not required for the initial preflight.

### AI coding agent

An AI coding agent is optional but recommended for the Agentic Power Hour. Aspire works with multiple agents, so use one you already have installed and authenticated, such as:

- [GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code/setup)
- [Codex CLI](https://developers.openai.com/codex/cli/)
- [OpenCode](https://opencode.ai/docs/)

Attendees without an agent can pair with another attendee or follow the instructor demonstration.

### Bring your own application

You are encouraged to bring an existing application that you are allowed to modify. Before the workshop:

- Confirm that it runs locally.
- Install the runtimes and package managers it needs.
- Confirm access to any private package feeds or development container registries.
- Prepare development-only credentials for any external services it requires.
- Do not use production credentials or sensitive customer data.

The workshop includes a complete bingo application for anyone who cannot or does not want to use their own codebase.

## Preflight checklist

Verify the required tools:

```bash
git --version
dotnet --version
node --version
npm --version
podman info
podman compose version
```

If you use Docker instead of Podman:

```bash
docker info
docker compose version
```

Clone the workshop repository and build the starting application:

```bash
git clone https://github.com/maddymontaquila/aspireify-workshop.git
cd aspireify-workshop

dotnet build demo/start/AspireifyBingo.slnx

cd demo/start/src/bingo-board
npm ci
npm run build
```

Pull the infrastructure images so they are available before the workshop:

```bash
podman pull postgres:17
podman pull redis:7
```

Docker users can replace `podman` with `docker`.

If you installed Aspire ahead of time, also run:

```bash
aspire --version
aspire doctor
```
