import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import AccountDeletingPage from "./AccountDeletingPage.jsx";

// 纯展示页冒烟：days_remaining 消费 location.state（DangerZone navigate 传入），
// 直达/刷新丢 state 时回退契约宽限期 14 天

function renderAt(entry) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <AccountDeletingPage />
    </MemoryRouter>
  );
}

describe("账户注销中冻结页（/account/deleting）", () => {
  it("location.state 携带 daysRemaining → 按传入值展示", () => {
    renderAt({ pathname: "/account/deleting", state: { daysRemaining: 9 } });

    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.getByText("账户注销中")).toBeTruthy();
    expect(screen.getByText(/9 天后生效/)).toBeTruthy();
  });

  it("直达/刷新丢 state → 回退契约默认 14 天", () => {
    renderAt("/account/deleting");

    expect(screen.getByText("账户注销中")).toBeTruthy();
    expect(screen.getByText(/14 天后生效/)).toBeTruthy();
  });

  it("文案钉死：恢复链接邮箱引导 + V1/V2 账户域区分", () => {
    renderAt("/account/deleting");

    expect(screen.getByText(/恢复链接已发送至邮箱/)).toBeTruthy();
    expect(screen.getByText(/邮件中的恢复链接/)).toBeTruthy();
    expect(screen.getByText(/旧版工作区账户/)).toBeTruthy();
    expect(screen.getByText(/不受影响/)).toBeTruthy();
  });
});
