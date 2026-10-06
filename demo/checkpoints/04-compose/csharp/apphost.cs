#:package Aspire.Hosting.Docker@13.6.0
#:package Aspire.Hosting.Yarp@13.6.0
#:sdk Aspire.AppHost.Sdk@13.6.0
#:property AspireUseCliBundle=true
#:property NoWarn=ASPIREPIPELINES003
#:package Aspire.Hosting.PostgreSQL@13.6.0
#:package Aspire.Hosting.Redis@13.6.0
#:package Aspire.Hosting.JavaScript@13.6.0
#:project ../../../start/src/BingoBoard.Admin/BingoBoard.Admin.csproj
#:project ../../../start/src/BingoBoard.MigrationService/BingoBoard.MigrationService.csproj

#pragma warning disable

using Aspire.Hosting.ApplicationModel;
using Aspire.Hosting.Pipelines;
using Aspire.Hosting.Publishing;
using Microsoft.Extensions.Logging;
using System.Runtime.InteropServices;

var builder = DistributedApplication.CreateBuilder(args);

builder.AddDockerComposeEnvironment("compose");

var adminPassword = builder.AddParameter("admin-password", secret: true);
var cache = builder.AddRedis("cache")
    .WithArgs("--maxmemory-policy", "allkeys-lfr");
var postgres = builder.AddPostgres("postgres").WithDataVolume();
var db = postgres.AddDatabase("db");

var migrations = builder.AddProject<Projects.BingoBoard_MigrationService>("migrations")
    .WithEnvironment("Authentication__AdminPassword", adminPassword)
    .WithEnvironment("Aspire__UseServiceDefaults", "true")
    .WithReference(db)
    .WaitFor(db);

var admin = builder.AddProject<Projects.BingoBoard_Admin>("boardadmin", launchProfileName: "http")
    .WithEnvironment("Aspire__UseServiceDefaults", "true")
    .WithEnvironment("HealthChecks__ExposeEndpoints", "true")
    .WithReference(cache)
    .WithReference(db)
    .WaitFor(cache)
    .WaitForCompletion(migrations)
    .WithHttpHealthCheck("/health")
    .WithExternalHttpEndpoints()
    .WithIconName("Trophy")
    .WithUrl("/", "Admin home")
    .WithUrl("/board-management", "Manage board")
    .WithUrl("/squares-management", "Manage squares");

var devFrontend = builder.AddViteApp("dev-frontend", "../../../start/src/bingo-board")
    .WithEnvironment("BINGO_ADMIN_URL", admin.GetEndpoint("http"))
    .WithEnvironment("__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS", "aspire.dev.internal")
    .WithHttpHealthCheck("/")
    .WithUrl("/", "Play bingo (Vite)")
    .WaitFor(admin);

// Choose exactly one proxy. Vite remains the default development frontend.
var frontend = AddYarpFrontend();
// var frontend = AddNginxFrontend();

frontend.WithExternalHttpEndpoints()
    .WithHttpHealthCheck("/")
    .WithUrl("/", "Play bingo")
    .WaitFor(admin);

if (builder.ExecutionContext.IsRunMode)
{
    frontend.WithExplicitStart();
}

var targetPlatform = RuntimeInformation.OSArchitecture switch
{
    Architecture.Arm64 => ContainerTargetPlatform.LinuxArm64,
    Architecture.X64 => ContainerTargetPlatform.LinuxAmd64,
    _ => throw new PlatformNotSupportedException("This checkpoint supports ARM64 and x64 hosts.")
};

foreach (var resource in new IResourceBuilder<IComputeResource>[] { migrations, admin, devFrontend, frontend })
{
    resource.WithContainerBuildOptions(options => options.TargetPlatform = targetPlatform);
}

builder.Pipeline.AddStep(
    "announce-images-built",
    context =>
    {
        context.Logger.LogInformation("All container images built successfully.");
        return Task.CompletedTask;
    },
    dependsOn: WellKnownPipelineSteps.Build,
    requiredBy: WellKnownPipelineSteps.Deploy);

builder.Build().Run();

IResourceBuilder<ContainerResource> AddYarpFrontend()
{
    var gateway = builder.AddYarp("bingoboard")
        .WithConfiguration(yarp =>
        {
            yarp.AddRoute("/api/version-info", admin);
            yarp.AddRoute("/bingohub/{**catch-all}", admin);
            if (builder.ExecutionContext.IsRunMode)
            {
                yarp.AddRoute(devFrontend);
            }
        })
        .PublishWithStaticFiles(devFrontend);

    if (builder.ExecutionContext.IsRunMode)
    {
        gateway.WaitFor(devFrontend);
    }

    return gateway;
}

IResourceBuilder<ContainerResource> AddNginxFrontend()
{
    devFrontend.ExcludeFromManifest();

    return builder.AddDockerfile("bingoboard", "../../../start/src/bingo-board", "Dockerfile.nginx")
        .WithHttpEndpoint(targetPort: 8080)
        .WithEnvironment("BINGO_ADMIN_URL", admin.GetEndpoint("http"));
}
