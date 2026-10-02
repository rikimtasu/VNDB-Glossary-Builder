import { invoke } from "@tauri-apps/api/core";

/** File I/O goes through Rust commands so no FS-scope allowlisting is needed. */
export const readText = (path: string) =>
  invoke<string>("read_text_file", { path });

export const writeText = (path: string, contents: string) =>
  invoke<void>("write_text_file", { path, contents });

export async function readBytes(path: string): Promise<Uint8Array> {
  const arr = await invoke<number[]>("read_binary_file", { path });
  return new Uint8Array(arr);
}

export const writeBytes = (path: string, data: Uint8Array) =>
  invoke<void>("write_binary_file", { path, data: Array.from(data) });

export interface DirEntry {
  name: string;
  is_dir: boolean;
}

export const listDir = (path: string) => invoke<DirEntry[]>("list_dir", { path });

export const makeDir = (path: string) => invoke<void>("create_dir_all", { path });
