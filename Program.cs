using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;

namespace AiWorkflowAutomation;

internal static class Program
{
    static async Task<int> Main(string[] args)
    {
        bool real = args.Contains("--real", StringComparer.OrdinalIgnoreCase);
        string ticketsPath = args.FirstOrDefault(a => a.EndsWith(".json", StringComparison.OrdinalIgnoreCase)) ?? "tickets.json";

        ILlm llm;
        if (real && Environment.GetEnvironmentVariable("ANTHROPIC_API_KEY") is { Length: > 0 })
        { Console.WriteLine("Using the Anthropic API (claude-sonnet-5-5).\n"); llm = new AnthropicLlm(); }
        else
        {
            if (real) Console.WriteLine("--real needs ANTHROPIC_API_KEY; using the offline mock.\n");
            else Console.WriteLine("Using the offline mock LLM (pass --real with ANTHROPIC_API_KEY for the real model).\n");
            llm = new MockLlm();
        }

        if (!File.Exists(ticketsPath)) { Console.Error.WriteLine($"error: {ticketsPath} not found"); return 1; }
        var tickets = JsonSerializer.Deserialize<List<Ticket>>(File.ReadAllText(ticketsPath),
                          new JsonSerializerOptions { PropertyNameCaseInsensitive = true }) ?? new();

        var workflow = new Workflow(new ClassifyStep(llm), new ExtractStep(llm), new DraftReplyStep(llm), new RouteStep());

        Console.WriteLine(new string('=', 76));
        Console.WriteLine($"  SUPPORT TICKET TRIAGE  —  {tickets.Count} tickets");
        Console.WriteLine(new string('=', 76));

        var byCategory = new Dictionary<string, int>();
        foreach (var ticket in tickets)
        {
            var ctx = new WorkflowContext { Ticket = ticket };
            await workflow.RunAsync(ctx);

            string cat = ctx.Data.GetValueOrDefault("category", "General");
            byCategory[cat] = byCategory.GetValueOrDefault(cat) + 1;

            Console.WriteLine($"\n[{ticket.Id}] {ticket.Subject}   ({ticket.From})");
            Console.WriteLine($"   category : {cat}");
            Console.WriteLine($"   urgency  : {ctx.Data.GetValueOrDefault("urgency")}");
            Console.WriteLine($"   route    : {ctx.Data.GetValueOrDefault("route")}");
            Console.WriteLine($"   extracted: {ctx.Data.GetValueOrDefault("extracted")}");
            Console.WriteLine("   draft reply:");
            foreach (var line in (ctx.Data.GetValueOrDefault("reply") ?? "").Split('\n'))
                Console.WriteLine($"     {line}");
        }

        Console.WriteLine("\n" + new string('=', 76));
        Console.WriteLine("  Summary by category: " + string.Join(",  ", byCategory.Select(kv => $"{kv.Key}={kv.Value}")));
        Console.WriteLine(new string('=', 76));
        return 0;
    }
}
