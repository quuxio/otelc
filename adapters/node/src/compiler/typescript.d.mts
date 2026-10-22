import type { ISourcePlan } from '../source-plan.ts';

/** Selected emit-only compiler profile; project type checking remains a build concern. */
export function compilerOptions(root?: string): object;
export function transpile(source: string, filename: string, sourceName: string, plan: ISourcePlan,
  root?: string, hintedFormat?: string): { code: string; map: object; format?: string };
