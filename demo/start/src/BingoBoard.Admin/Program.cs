using Microsoft.AspNetCore.Identity;
using Microsoft.AspNetCore.Components;
using BingoBoard.Admin.Components;
using BingoBoard.Admin.Endpoints;
using BingoBoard.Admin.Hubs;
using BingoBoard.Admin.Services;
using BingoBoard.Data;
using Microsoft.EntityFrameworkCore;
using Scalar.AspNetCore;

var builder = WebApplication.CreateBuilder(args);
var useServiceDefaults = builder.Configuration.GetValue<bool>("Aspire:UseServiceDefaults");

if (useServiceDefaults)
{
    builder.AddServiceDefaults();
}

var databaseConnection = builder.Configuration.GetConnectionString("db")
    ?? throw new InvalidOperationException("Connection string 'db' is required.");
var cacheConnection = builder.Configuration.GetConnectionString("cache")
    ?? throw new InvalidOperationException("Connection string 'cache' is required.");

builder.Services.AddAuthentication(options =>
    {
        options.DefaultScheme = IdentityConstants.ApplicationScheme;
        options.DefaultSignInScheme = IdentityConstants.ExternalScheme;
    })
    .AddIdentityCookies();
builder.Services.AddAuthorization();

// Configure OpenAPI support
builder.Services.AddOpenApi();
// Add validation support
builder.Services.AddValidation();

builder.Services.AddDbContext<ApplicationDbContext>(options =>
    options.UseNpgsql(databaseConnection));

builder.Services.AddDefaultIdentity()
    .AddSignInManager()
    .AddDefaultTokenProviders();

builder.Services.ConfigureApplicationCookie(options =>
{
    options.LoginPath = "/login";
    options.LogoutPath = "/logout";
});

// Add services to the container.
builder.Services.AddRazorComponents()
    .AddInteractiveServerComponents();

builder.Services.AddCascadingAuthenticationState();
builder.Services.AddScoped<RedirectManager>();

// Add SignalR
builder.Services.AddSignalR()
    .AddStackExchangeRedis(cacheConnection);

builder.Services.AddStackExchangeRedisCache(options =>
    options.Configuration = cacheConnection);

// Register custom services
builder.Services.AddScoped<IBingoService, BingoService>();
builder.Services.AddScoped<IClientConnectionService, ClientConnectionService>();

// Add HttpClient for API calls within the app
builder.Services.AddScoped(sp =>
{
    var httpClient = new HttpClient
    {
        BaseAddress = new Uri(sp.GetRequiredService<NavigationManager>().BaseUri)
    };
    return httpClient;
});

// Register background services
builder.Services.AddHostedService<ApprovalCleanupService>();

builder.Services.AddSingleton<AddressResolver>();
builder.Services.AddSingleton<AppVersionInfoProvider>();

// Add logging
builder.Services.AddLogging();

var app = builder.Build();

// Configure the HTTP request pipeline.
if (!app.Environment.IsDevelopment())
{
    app.UseExceptionHandler("/Error", createScopeForErrors: true);
    // The default HSTS value is 30 days. You may want to change this for production scenarios, see https://aka.ms/aspnetcore-hsts.
    app.UseHsts();
}

app.UseStatusCodePagesWithReExecute("/not-found", createScopeForStatusCodePages: true);
app.UseHttpsRedirection();

if (app.Environment.IsDevelopment())
{
    app.MapOpenApi();
    app.MapScalarApiReference();
    app.UseSwaggerUI(options =>
    {
        options.SwaggerEndpoint("/openapi/v1.json", "Bingo Board Admin API");
        options.RoutePrefix = "swagger";
    });
    app.MapProducerEndpoints();
}

app.UseStaticFiles();
app.MapStaticAssets();

app.UseAuthentication();
app.UseAuthorization();
app.UseAntiforgery();

// Map Razor components
app.MapRazorComponents<App>()
    .AddInteractiveServerRenderMode();

// Map SignalR hub without authentication (anonymous access allowed)
app.MapHub<BingoHub>("/bingohub");

// Map authentication endpoints
app.MapAuthenticationEndpoints();

app.MapGet("/api/version", (AppVersionInfoProvider versionInfoProvider) => versionInfoProvider.GetVersionInfo());

if (useServiceDefaults)
{
    app.MapDefaultEndpoints();
}

app.Run();
