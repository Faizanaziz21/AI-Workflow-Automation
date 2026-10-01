using System;
using System.Collections.Generic;
using System.Threading.Tasks;

namespace AiWorkflowAutomation;

/// <summary>An incoming support ticket (the unit of work for the pipeline).</summary>
public record Ticket(string Id, string From, string Subject, string Body);

/// <summary>Shared state threaded through the steps: the ticket plus each step's output.</summary>
public sealed class WorkflowContext
{
    public required Ticket Ticket { get; init; }
    public Dictionary<string, string> Data { get; } = new(StringComparer.OrdinalIgnoreCase);
}

/// <summary>One stage in the automation. Reads/writes the context; may or may not call the LLM.</summary>
public interface IWorkflowStep
{
    string Name { get; }
    Task RunAsync(WorkflowContext ctx);
}

/// <summary>
/// Runs a fixed sequence of steps over a context. A real "AI workflow automation" is just this:
/// deterministic orchestration around LLM-powered steps, with each step's output feeding the next.
/// </summary>
public sealed class Workflow
{
    private readonly IReadOnlyList<IWorkflowStep> _steps;
    public Workflow(params IWorkflowStep[] steps) => _steps = steps;

    public async Task RunAsync(WorkflowContext ctx, Action<string>? log = null)
    {
        foreach (var step in _steps)
        {
            try { await step.RunAsync(ctx); }
            catch (Exception ex) { ctx.Data[step.Name + "_error"] = ex.Message; log?.Invoke($"    ! {step.Name} failed: {ex.Message}"); }
        }
    }
}
