# 白天续做卡：把 mcd-dine 推上 GitHub（3 步，约 2 分钟）

> 写于 2026-10-23 00:1x。停在这里的原因：**本机与老板的浏览器都打不开 github.com**
> （`github.com:443` 间歇性整体超时，非 404／非账号问题）。白天网络恢复后照这页做即可，
> **不需要任何历史对话**。

## 现状（已到位，不用重做）

| 项 | 值 |
|---|---|
| GitHub 仓库 | **https://github.com/langlangagowith/mcd-dine**（Public，已创建，空的） |
| 本地仓库 | `D:\mcd-dine`，分支 **`main`**，2 笔提交（`2b2c4cc` / `3419fb2`） |
| SSH 密钥 | `C:\Users\Administrator\.ssh\mcd_dine`（ed25519，无口令）已生成 |
| SSH 配置 | `~/.ssh/config`：`Host github.com → HostName ssh.github.com / Port 443 / IdentityFile mcd_dine` |
| remote | `ssh://git@ssh.github.com:443/langlangagowith/mcd-dine.git`（已 add） |

## 第 1 步：注册公钥（唯一需要人手的一步）

打开 **https://github.com/settings/ssh/new** → Title 填 `mcd-dine-workbuddy` → Key type 保持 `Authentication Key` → 粘贴：

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE56XP6kQ/CPtzkQpHzUHCTfV10cNxPMznYWZ0MhnHpl mcd-dine@workbuddy
```

（同一内容备份在 `D:\mcd-dine\_probe\ssh_pub_mcd_dine.txt`）

## 第 2 步：推（我来跑，一条命令）

```bash
cd /d/mcd-dine
git push -u origin main
```

**判据**：返回 `Permission denied (publickey)` = 公钥还没生效（刷新页面确认已 Add，别重试超过 2 次）；
返回 `Could not resolve hostname` / 长时间无响应 = 网络又挂了，改试 `git push` 走 ssh.github.com:443（已配）。

## 第 3 步：比赛提交（10-24 10:30 开闸，10-25 16:00 截止）

`https://1024.waytoagi.com/contest/submission` → 赛道「用餐」→ 链接填仓库 URL →
使用工具勾 **WorkBuddy**（赛道前三评选强制要求）→ 演示录屏选填（建议录 30s：改人数/预算 → 券省多少 → 依据链展开）。
提交前确认仓库里 `workbuddy.md` 在根目录（**官方硬要求：公开仓库 + 内含 workbuddy.md**）。

## 备用方案（若 SSH 一直不顺）

github.com 网页能开时，生成 classic PAT（勾 `repo`）→
`git -c credential.helper= push https://<user>:<PAT>@github.com/langlangagowith/mcd-dine.git main`
（`-c credential.helper=` 是防沙箱 helper 挂死的必要参数，见 `tools-github-access.md` §三）。

## 别忘了

- 10-24 10:30 后 `https://1024.waytoagi.com/me/benefits` 领 TokLens 兑换码（**当天兑，3 天内用完**）。
- 提交一旦审核通过，撤回重交不重发兑换码 ⇒ 提交前先本地自查一遍。
