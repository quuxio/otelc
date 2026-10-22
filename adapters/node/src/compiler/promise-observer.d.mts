import type { IAsyncSite } from '../source-plan.ts';

export interface INativePromiseObserver {
  state(value: Promise<unknown>): 0 | 1 | 2;
  frames(): Array<[string, number, number, number]>;
  checkpoint(callback: () => void): number;
  stopCheckpoint(generation: number): boolean;
  checkpointStats(): { active: boolean; generation: number; failures: number };
}
export function nativeObserver(): INativePromiseObserver;

interface IObserverRuntime {
  readonly observing: boolean;
  readonly closed: boolean;
  readonly plan: { runtime: { max_functions: number; max_active_calls: number } };
  readonly losses: { invalid: number; function_capacity: number; active_call_capacity: number; async_origin: number };
  enter(name: string): number;
  exit(token: number, unwound: boolean, ended?: bigint | null): void;
  rejectTrace(reason: string): number;
}
export class PromiseObserver {
  constructor(runtime: IObserverRuntime);
  register(sites: IAsyncSite[]): void;
  enter(name: string, id: number): number;
  flush(): void;
  close(): void;
  failures(): number;
}
