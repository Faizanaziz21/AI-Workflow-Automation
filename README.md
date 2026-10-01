# AI Workflow Automation — support-ticket triage (C# / .NET)

An **AI-powered workflow engine** that chains LLM-backed steps into an automation. The included
pipeline triages support tickets end-to-end: **classify → extract → draft reply → route**, with
each step's output feeding the next. The engine is generic — swap the steps for any multi-stage
AI automation (document processing, lead qualification, content pipelines, etc.).

## Sample run (offline mock)

```
============================================================================
  SUPPORT TICKET TRIAGE  —  4 tickets
============================================================================

[T-1001] Refund not received   (jane@example.com)
   category : Billing
   urgency  : High
   route    : PRIORITY → Billing team
   extracted: {"order_id":"48213"}
   draft reply:
     Hi,
     Thanks for reaching out. I've flagged your billing/refund issue for our finance
     team and we'll confirm the status shortly.
     Best regards, Support Team

[T-1002] App keeps crashing   (mike@acme.co)
   category : Technical     urgency : Medium     route : Technical team
   extracted: {"error_code":"0xC0000005"}
   ...
```

## How it works

- **Workflow engine** (`Workflow.cs`) — runs a sequence of `IWorkflowStep`s over a shared
  `WorkflowContext`, threading each step's output to the next, with per-step error isolation.
- **Steps** (`Steps.cs`):
  1. `ClassifyStep` — LLM → `{category, urgency}`
  2. `ExtractStep` — LLM → structured fields (`order_id`, `error_code`, `plan`)
  3. `DraftReplyStep` — LLM → a short customer reply
  4. `RouteStep` — rule-based → a queue (priority-flagged for High urgency)
- **Pluggable LLM** (`ILlm`):
  - `AnthropicLlm` — real Anthropic Messages API (`claude-sonnet-5-5`, reads `ANTHROPIC_API_KEY`)
  - `MockLlm` — deterministic offline stand-in so the whole pipeline runs with no key

Because the LLM is behind an interface, the demo runs offline and switches to the real model by
changing one flag.

## Run

```
dotnet run                 # offline mock over tickets.json
dotnet run -- --real       # real model (needs ANTHROPIC_API_KEY)
dotnet run -- mine.json     # your own tickets
```

## Build

```
dotnet build -c Release
```

Requires the .NET SDK. No external NuGet packages.
