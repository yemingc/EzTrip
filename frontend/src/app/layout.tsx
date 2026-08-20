import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "EzTrip | 可解释的多 Agent 旅行规划",
  description:
    "面向中国用户的多 Agent 旅行规划与行程变化处理项目。",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
