#:sdk Aspire.AppHost.Sdk@13.6.0
#:property AspireUseCliBundle=true
#:package Aspire.Hosting.PostgreSQL@13.6.0
#:package Aspire.Hosting.Redis@13.6.0
#:package Aspire.Hosting.JavaScript@13.6.0
#:project ../../../start/src/BingoBoard.Admin/BingoBoard.Admin.csproj
#:project ../../../start/src/BingoBoard.MigrationService/BingoBoard.MigrationService.csproj

using System.Net.Http.Json;
using Aspire.Hosting.ApplicationModel;

var builder = DistributedApplication.CreateBuilder(args);

var adminPassword = builder.AddParameter("admin-password", secret: true);

var cache = builder.AddRedis("cache")
    .WithArgs("--maxmemory-policy", "allkeys-lfr");

var db = builder.AddPostgres("postgres")
    .WithDataVolume()
    .AddDatabase("db");

var migrations = builder.AddProject<Projects.BingoBoard_MigrationService>("migrations")
    .WithEnvironment("Authentication__AdminPassword", adminPassword)
    .WithEnvironment("Aspire__UseServiceDefaults", "true")
    .WithReference(db)
    .WaitFor(db);

var admin = builder.AddProject<Projects.BingoBoard_Admin>("boardadmin", launchProfileName: "http")
    .WithEnvironment("Authentication__AdminPassword", adminPassword)
    .WithEnvironment("Aspire__UseServiceDefaults", "true")
    .WithReference(cache)
    .WithReference(db)
    .WaitFor(cache)
    .WaitForCompletion(migrations)
    .WithHttpHealthCheck("/health")
    .WithExternalHttpEndpoints()
    .WithIconName("Trophy")
    .WithUrl("/", "Admin home")
    .WithUrl("/board-management", "Manage board")
    .WithUrl("/squares-management", "Manage squares")
    .WithHttpCommand(
        path: "/api/demo/producer/status",
        displayName: "Check admin status",
        commandName: "check-status",
        commandOptions: new HttpCommandOptions
        {
            Method = HttpMethod.Get,
            Description = "Report connected players, pending approvals, and called squares.",
            ResultMode = HttpCommandResultMode.Json
        })
    .WithHttpCommand(
        path: "/api/demo/producer/squares/import",
        displayName: "Add bingo square",
        commandName: "add-bingo-square",
        commandOptions: new HttpCommandOptions
        {
            Method = HttpMethod.Post,
            Description = "Add or update a square through the admin application's developer API.",
            ConfirmationMessage = "Add this square to the bingo board?",
            IconName = "AddSquare",
            IsHighlighted = true,
            ResultMode = HttpCommandResultMode.Auto,
            Arguments =
            [
                new InteractionInput
                {
                    Name = "id",
                    Label = "Square ID",
                    InputType = InputType.Text,
                    Required = true,
                    MaxLength = 100
                },
                new InteractionInput
                {
                    Name = "label",
                    Label = "Square text",
                    InputType = InputType.Text,
                    Required = true,
                    MaxLength = 200
                },
                new InteractionInput
                {
                    Name = "category",
                    Label = "Category",
                    InputType = InputType.Text,
                    Value = "workshop",
                    MaxLength = 50
                }
            ],
            PrepareRequest = context =>
            {
                context.Request.Content = JsonContent.Create(new[]
                {
                    new
                    {
                        Id = context.Arguments.GetString("id")!,
                        Label = context.Arguments.GetString("label")!,
                        Type = context.Arguments.GetString("category"),
                        IsActive = true
                    }
                });

                return Task.CompletedTask;
            }
        });

var frontend = builder.AddViteApp("bingoboard", "../../../start/src/bingo-board")
    .WithEnvironment("BINGO_ADMIN_URL", admin.GetEndpoint("http"))
    .WithReference(admin)
    .WithUrl("/", "Play bingo")
    .WithHttpHealthCheck("/")
    .WaitFor(admin);

frontend.WithCommand(
    name: "demo-links",
    displayName: "Generate demo links",
    executeCommand: _ =>
    {
        var playerUrl = frontend.GetEndpoint("http").Url;
        var adminUrl = admin.GetEndpoint("http").Url;
        var links = $"""
            ## Bingo demo links

            - [Play bingo]({playerUrl})
            - [Admin home]({adminUrl})
            - [Manage the board]({adminUrl}/board-management)
            - [Manage squares]({adminUrl}/squares-management)
            """;

        return Task.FromResult(CommandResults.Success(
            "Demo links ready.",
            links,
            CommandResultFormat.Markdown,
            displayImmediately: true));
    },
    commandOptions: new CommandOptions
    {
        Description = "Generate a facilitator cheat sheet from Aspire's allocated endpoints.",
        IconName = "LinkMultiple"
    });

builder.Build().Run();
