import type { ICompilerEvidence, ISourcePlan } from '../source-plan.ts';

/** Selected emit-only compiler profile; project type checking remains a build concern. */
export function compilerOptions(root?: string, readFile?: (filename: string) => string | undefined): object;
export function transpile(source: string, filename: string, sourceName: string, plan: ISourcePlan,
  root?: string, hintedFormat?: string,
  readFile?: (filename: string) => string | undefined): { code: string; map: object; format?: string; compilation: ICompilerEvidence };
