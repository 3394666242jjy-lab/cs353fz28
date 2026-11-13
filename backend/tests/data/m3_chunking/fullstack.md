# 合成全栈工程知识案例

本文件用于测试知识块结构，不代表已上线系统或真实个人项目。

## React 状态

将组件需要共享的最小状态放在共同上层，通过 props 传入子组件。能从现有状态计算的展示结果，不必再保存一份。组件负责呈现加载、错误和成功状态；业务权限仍由服务端校验。切换账号时要清理属于旧账号的本地数据，不能只更新头像和名称。

## Fetch 错误处理

HTTP 404 响应仍可能成功返回 Response 对象，因此需要检查 response.ok。网络错误、业务错误和主动取消应分别处理。过期请求返回时不能覆盖当前页面的数据；把 AbortController 的 signal 传给 fetch，可以在页面退出时取消读取。

```typescript
// 合成示例：空行仍属于同一个代码块。
async function loadRecord(url: string, signal: AbortSignal) {
  const response = await fetch(url, { signal });

  // 这一段不是新的 Markdown 小节。
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  const record = await response.json();
  return record;
}
```

## 事务与消息

业务记录与 Outbox 指令应在同一数据库事务中保存，事务提交后再投递消息。发布消息成功但来不及更新发布标记时，可能重复投递；消费端需要状态版本或幂等键。数据库事务不会自动把外部 HTTP 请求或消息系统中的所有副作用一起撤销。

## Dockerfile 与运行

镜像构建指令和容器默认启动命令有不同生命周期。构建时需要固定基础镜像和依赖，容器启动时注入运行配置。不要把开发密钥复制进镜像。这个合成示例用容器启动后的健康检查与业务请求共同验证服务，而不是只看构建命令退出码。
