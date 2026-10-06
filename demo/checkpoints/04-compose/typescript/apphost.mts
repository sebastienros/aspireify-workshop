import { ContainerTargetPlatform, createBuilder } from './.aspire/modules/aspire.mjs';

const builder = await createBuilder();

await builder.addDockerComposeEnvironment('compose');

const adminPassword = await builder.addParameter('admin-password', { secret: true });
const cache = await builder.addRedis('cache')
  .withArgs(['--maxmemory-policy', 'allkeys-lfr']);
const postgres = await builder.addPostgres('postgres').withDataVolume();
const db = await postgres.addDatabase('db');

const migrations = await builder
  .addProject('migrations', '../../../start/src/BingoBoard.MigrationService/BingoBoard.MigrationService.csproj')
  .withEnvironment('Authentication__AdminPassword', adminPassword)
  .withEnvironment('Aspire__UseServiceDefaults', 'true')
  .withReference(db)
  .waitFor(db);

const admin = await builder
  .addProject('boardadmin', '../../../start/src/BingoBoard.Admin/BingoBoard.Admin.csproj')
  .withEnvironment('Aspire__UseServiceDefaults', 'true')
  .withEnvironment('HealthChecks__ExposeEndpoints', 'true')
  .withReference(cache)
  .withReference(db)
  .waitFor(cache)
  .waitForCompletion(migrations)
  .withHttpHealthCheck({ path: '/health' })
  .withExternalHttpEndpoints()
  .withIconName('Trophy')
  .withUrl('/', { displayText: 'Admin home' })
  .withUrl('/board-management', { displayText: 'Manage board' })
  .withUrl('/squares-management', { displayText: 'Manage squares' });

const devFrontend = await builder
  .addViteApp('dev-frontend', '../../../start/src/bingo-board')
  .withEnvironment('BINGO_ADMIN_URL', await admin.getEndpoint('http'))
  .withEnvironment('__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS', 'aspire.dev.internal')
  .withHttpHealthCheck({ path: '/' })
  .withUrl('/', { displayText: 'Play bingo (Vite)' })
  .waitFor(admin);

// Choose exactly one proxy. Vite remains the default development frontend.
const frontend = await addYarpFrontend();
// const frontend = await addNginxFrontend();

await frontend.withExternalHttpEndpoints()
  .withHttpHealthCheck({ path: '/' })
  .withUrl('/', { displayText: 'Play bingo' })
  .waitFor(admin);

if (await builder.executionContext().isRunMode()) {
  await frontend.withExplicitStart();
}

if (process.arch !== 'arm64' && process.arch !== 'x64') {
  throw new Error('This checkpoint supports ARM64 and x64 hosts.');
}
const targetPlatform = process.arch === 'arm64'
  ? ContainerTargetPlatform.LinuxArm64
  : ContainerTargetPlatform.LinuxAmd64;

for (const resource of [migrations, admin, devFrontend, frontend]) {
  await resource.withContainerBuildOptions(async (options) => {
    await options.targetPlatform.set(targetPlatform);
  });
}

await builder.build().run();

async function addYarpFrontend() {
  const gateway = await builder.addYarp('bingoboard');
  await gateway.withConfiguration(async (yarp) => {
    await yarp.addRoute('/api/version-info', admin);
    await yarp.addRoute('/bingohub/{**catch-all}', admin);
    if (await builder.executionContext().isRunMode()) {
      await yarp.addCatchAllRoute(devFrontend);
    }
  });
  await gateway.publishWithStaticFiles(devFrontend);
  if (await builder.executionContext().isRunMode()) {
    await gateway.waitFor(devFrontend);
  }
  return gateway;
}

async function addNginxFrontend() {
  await devFrontend.excludeFromManifest();

  return builder
    .addDockerfile('bingoboard', '../../../start/src/bingo-board', { dockerfilePath: 'Dockerfile.nginx' })
    .withHttpEndpoint({ targetPort: 8080 })
    .withEnvironment('BINGO_ADMIN_URL', await admin.getEndpoint('http'));
}
