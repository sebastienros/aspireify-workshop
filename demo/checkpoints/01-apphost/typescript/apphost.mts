import { createBuilder } from './.aspire/modules/aspire.mjs';

const builder = await createBuilder();

const cache = await builder.addRedis('cache')
  .withArgs(['--maxmemory-policy', 'allkeys-lfr']);

const postgres = await builder
  .addPostgres('postgres')
  .withDataVolume();
const db = await postgres.addDatabase('db');

const migrations = await builder
  .addProject('migrations', '../../../start/src/BingoBoard.MigrationService/BingoBoard.MigrationService.csproj')
  .withEnvironment('Authentication__AdminPassword', 'admin')
  .withReference(db)
  .waitFor(db);

const admin = await builder
  .addProject('boardadmin', '../../../start/src/BingoBoard.Admin/BingoBoard.Admin.csproj')
  .withReference(cache)
  .withReference(db)
  .waitFor(cache)
  .waitForCompletion(migrations)
  .withExternalHttpEndpoints();

await builder
  .addViteApp('bingoboard', '../../../start/src/bingo-board')
  .withEnvironment('BINGO_ADMIN_URL', await admin.getEndpoint('http'))
  .withReference(admin)
  .waitFor(admin);

await builder.build().run();
