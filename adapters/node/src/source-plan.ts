import type { ISdkPolicy } from './sdk-pipeline.ts';
import type { IPatterns } from './selection.ts';

/** The common policy after canonical Python validation, plus generated launch metadata. */
export interface ISourcePlan extends ISdkPolicy {
  schema_version: 3;
  languages: string[];
  language: 'javascript' | 'typescript';
  backend: 'native' | 'api';
  plain?: boolean;
  launch_id: string;
  sources: IPatterns;
  functions: IPatterns;
  context: { tasks: boolean; http: boolean };
  lifetimes: IPatterns & { enabled: boolean; max_live: number; release_methods: string[] };
  source_matchers: IPatterns;
  function_matchers: IPatterns;
  lifetime_matchers: IPatterns;
  annotations: { read_existing: boolean; inject_generated: boolean };
  runtime: { root: string; directory: string; max_functions: number; max_active_calls: number };
}

export interface IAsyncSite {
  id: number; name: string; filename: string;
  start: [number, number]; end: [number, number]; origin: [number, number];
}

export interface ICompilerFile {
  path?: string;
  text: string;
}

/** Exact arguments and byte strings at the qualified compiler invocation boundary. */
export interface ICompilerEvidence {
  backend: 'native' | 'api';
  version: string;
  compiler_path: string;
  cwd: string;
  argv?: string[];
  method?: 'transpileModule';
  transformer_profile: 'none' | 'original-identities-v1';
  files: Record<'input' | 'options' | 'output' | 'map', ICompilerFile> & { preparation_map?: ICompilerFile };
}
