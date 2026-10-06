using BingoBoard.MigrationService;
using BingoBoard.Data;
using Microsoft.EntityFrameworkCore;
using OpenTelemetry.Trace;

var builder = Host.CreateApplicationBuilder(args);

if (builder.Configuration.GetValue<bool>("Aspire:UseServiceDefaults"))
{
    builder.AddServiceDefaults();
    builder.Services.AddOpenTelemetry()
        .WithTracing(tracing => tracing.AddSource(Worker.ActivitySourceName));
}

builder.Services.AddHostedService<Worker>();

var databaseConnection = builder.Configuration.GetConnectionString("database")
    ?? throw new InvalidOperationException("Connection string 'database' is required.");

builder.Services.AddDbContext<ApplicationDbContext>(options =>
    options.UseNpgsql(databaseConnection, npgsql => npgsql.EnableRetryOnFailure()));
builder.Services.AddDefaultIdentity();

var host = builder.Build();
host.Run();
