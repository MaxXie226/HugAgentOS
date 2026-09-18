/**
 * 「当前开着哪个项目」只有一份，就在 projectStore 里（进入 `/projects/<项目id>` 时由
 * 地址给出）。这里不存值，只是一个读取入口，给不能直接 import projectStore 的模块用
 * （projectStore → chatStore 已经是一条依赖边，反向直接 import 会成环）。
 */
let read: () => string | null = () => null;

export function setActiveProjectReader(fn: () => string | null): void {
  read = fn;
}

export function activeProjectId(): string | null {
  return read();
}
