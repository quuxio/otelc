import type { IAsyncSite, ISourcePlan } from '../source-plan.ts';

interface IInventoryItem { name: string; selected: boolean; line: number }
interface IPreparedSource { code: string; map: object; format?: string }
interface ICompilerRuntime { register(name: string): boolean; registerAsyncSites?(sites: IAsyncSite[]): void }
interface ITransformedSource { code: string; functions: IInventoryItem[]; types: IInventoryItem[]; format: string }

/** Transform a compiler-emitted or original source without writing application files. */
export function transform(source: string, filename: string, sourceName: string, plan: ISourcePlan,
  runtime: ICompilerRuntime, prepared?: IPreparedSource | null): ITransformedSource;
export const MARKER: string;
export const IDENTITY: string;
