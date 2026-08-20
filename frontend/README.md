# EzTrip frontend

Next.js App Router 前端。Gate 0 只提供项目定位页与完整质量检查脚本，尚未宣称旅行规划功能已经完成。

## 本地启动

```powershell
Copy-Item .env.example .env.local
pnpm install
pnpm dev
```

浏览器访问 `http://localhost:3000`。

## 质量检查

```powershell
pnpm lint
pnpm typecheck
pnpm build
```
