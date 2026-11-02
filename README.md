# Story Runner

The Two Fridays demo page plus a small backend that holds lab API tokens.

## Layout
    story-runner/          Two Fridays + landing page (port 80)
    story-runner/ai-basics AI Basics Lab (port 3100), unpacked from ai-basics-deploy.tar.gz

## Start
    cp .env.example .env
    docker compose up -d --build
Open http://<vm-ip>

## After changing .env or files
    docker compose up -d --build

## Logs
    docker compose logs -f

## Stop
    docker compose down
