#:sdk Aspire.AppHost.Sdk@13.6.0
#:property AspireUseCliBundle=true
#:package Aspire.Hosting.PostgreSQL@13.6.0
#:package Aspire.Hosting.Redis@13.6.0
#:package Aspire.Hosting.JavaScript@13.6.0
#:project ../../../start/src/BingoBoard.Admin/BingoBoard.Admin.csproj
#:project ../../../start/src/BingoBoard.MigrationService/BingoBoard.MigrationService.csproj

var builder = DistributedApplication.CreateBuilder(args);

var cache = builder.AddRedis("cache")
    .WithArgs("--maxmemory-policy", "allkeys-lfr");

var db = builder.AddPostgres("postgres")
    .WithDataVolume()
    .AddDatabase("db");

var migrations = builder.AddProject<Projects.BingoBoard_MigrationService>("migrations")
    .WithEnvironment("Authentication__AdminPassword", "admin")
    .WithReference(db)
    .WaitFor(db);

var admin = builder.AddProject<Projects.BingoBoard_Admin>("boardadmin")
    .WithReference(cache)
    .WithReference(db)
    .WaitFor(cache)
    .WaitForCompletion(migrations)
    .WithExternalHttpEndpoints();

builder.AddViteApp("bingoboard", "../../../start/src/bingo-board")
    .WithEnvironment("BINGO_ADMIN_URL", admin.GetEndpoint("http"))
    .WithReference(admin)
    .WithUrl("/admin", "admin")
    .WaitFor(admin);

builder.Build().Run();
