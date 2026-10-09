import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { test } from 'node:test';
import ts from 'typescript';
import { plan, receiver, root } from './helpers.mjs';

const execute = promisify(execFile);
test('both TypeScript compilers preserve package/extension module semantics and selected calls', async () => {
  const collector = await receiver();
  const source = 'import {join} from "node:path";\nfunction selected(value:number){return value*3;}\nconsole.log(join("a","b"),selected(7));\n';
  try {
    for (const backend of ['source', 'native']) {
      for (const [extension, type, module] of [['ts','commonjs','CommonJS'], ['ts','commonjs','NodeNext'], ['ts','module','NodeNext'], ['cts','module','CommonJS'], ['mts','commonjs','ESNext'], ['ts','commonjs',undefined]]) {
        const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-typescript-module-'));
        try {
          const filename = 'main.' + extension, report = path.join(folder,'report.json');
          const config = JSON.stringify({compilerOptions:{target:'ES2022', ...(module ? {module} : {})}});
          fs.writeFileSync(path.join(folder,'package.json'),JSON.stringify({type}));
          fs.writeFileSync(path.join(folder,'tsconfig.json'), config);
          fs.writeFileSync(path.join(folder,filename),source);
          const policy = {...plan(collector.endpoint),language:'typescript',backend};
          policy.function_matchers.include=['(?-u)^main\\.selected$'];
          fs.writeFileSync(path.join(folder,'plan.json'),JSON.stringify(policy));
          // Independent ordinary compiler control, without adapter hooks or probes.
          const commonjs=extension==='cts' || extension==='ts' && type==='commonjs';
          const baseline='baseline.'+(commonjs?'cjs':'mjs');
          fs.writeFileSync(path.join(folder,baseline),ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:commonjs?ts.ModuleKind.CommonJS:ts.ModuleKind.ESNext}}).outputText);
          const ordinary=await execute(process.execPath,[baseline],{cwd:folder,timeout:20000});
          const plain = await execute(process.execPath,['--import',path.join(root,'adapters/node/plain.mjs'),filename],{cwd:folder,env:{...process.env,OTELC_TYPESCRIPT_BACKEND:backend},timeout:20000});
          const measured = await execute(process.execPath,['--import',path.join(root,'adapters/node/register.mjs'),filename],{cwd:folder,env:{...process.env,OTELC_NODE_PLAN:path.join(folder,'plan.json'),OTELC_REPORT_PATH:report},timeout:20000});
          assert.deepEqual(plain,ordinary);
          assert.deepEqual(measured,plain,backend+' '+filename+' '+type+' '+module);
          assert.equal(measured.stdout, 'a/b 21\n'); assert.equal(measured.stderr,'');
          const health=JSON.parse(fs.readFileSync(report)); assert.equal(health.function_calls,1);assert.equal(health.export_loss,0);assert.equal(health.export_finished,true);
          assert.equal(fs.readFileSync(path.join(folder,filename),'utf8'),source);assert.equal(fs.readFileSync(path.join(folder,'tsconfig.json'),'utf8'),config);
        } finally {fs.rmSync(folder,{recursive:true});}
      }
    }
  } finally {await collector.close();}
});
