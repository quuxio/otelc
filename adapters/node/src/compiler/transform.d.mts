import type { IAsyncSite, ISourcePlan } from '../source-plan.ts';

interface IInventoryItem { name: string; selected: boolean; line: number }
interface IPreparedSource { code: string; map: object; format?: string }
interface ICompilerRuntime { register(name: string): boolean; registerAsyncSites?(sites: IAsyncSite[]): void }
interface ITransformedSource { code: string; functions: IInventoryItem[]; types: IInventoryItem[]; format: string }

/** Transform a compiler-emitted or original source without writing application files. */
export function transform(source: string, filename: string, sourceName: string, plan: ISourcePlan,
  runtime: ICompilerRuntime, prepared?: IPreparedSource | null, formatHint?: string, loadId?: number): ITransformedSource;
/** Compose a metadata-only module-body observation with an actual compiler map. */
export function observeModuleEntry(source: string, filename: string, map: object, format: string,
  loadId: number): { code: string; map: object };
export const MARKER: string;
export const IDENTITY: string;
