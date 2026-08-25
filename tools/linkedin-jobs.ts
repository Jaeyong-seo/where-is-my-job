#!/usr/bin/env bun
/**
 * LinkedIn 채용공고 수집기 — 로그인 불필요 (guest API 사용)
 *
 * 사용법:
 *   bun tools/linkedin-jobs.ts search "founding engineer" --location "Toronto, Ontario, Canada" [--remote] [--days 7] [--pages 2]
 *   bun tools/linkedin-jobs.ts job <jobId|url>            # 상세 JD를 마크다운으로 출력
 *   bun tools/linkedin-jobs.ts inbox <jobId|url>          # jobs/inbox/ 에 초안 파일 생성
 *
 * 엔드포인트 (비로그인 공개):
 *   - 검색: /jobs-guest/jobs/api/seeMoreJobPostings/search
 *   - 상세: /jobs-guest/jobs/api/jobPosting/{id}
 * 429 뜨면 자동 백오프. 과도한 호출(수백 req/분)은 IP 차단될 수 있으니 pages는 적당히.
 */

const BASE = "https://www.linkedin.com/jobs-guest/jobs/api";
const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function fetchHtml(url: string, tries = 4): Promise<string> {
  for (let i = 0; i < tries; i++) {
    const res = await fetch(url, {
      headers: { "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9" },
    });
    if (res.ok) return res.text();
    if (res.status === 429 || res.status >= 500) {
      const wait = 2000 * (i + 1);
      console.error(`  HTTP ${res.status}, ${wait}ms 대기 후 재시도…`);
      await sleep(wait);
      continue;
    }
    throw new Error(`HTTP ${res.status} for ${url}`);
  }
  throw new Error(`재시도 초과: ${url}`);
}

const decode = (s: string) =>
  s
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)))
    .trim();

interface JobCard {
  id: string;
  title: string;
  company: string;
  location: string;
  posted: string;
  url: string;
}

function parseCards(html: string): JobCard[] {
  const cards: JobCard[] = [];
  // 각 결과는 <li> 블록. base-card 단위로 자른다.
  const blocks = html.split(/<li[\s>]/).slice(1);
  for (const b of blocks) {
    const id =
      b.match(/data-entity-urn="urn:li:jobPosting:(\d+)"/)?.[1] ??
      b.match(/-(\d+)\?refId=/)?.[1] ??
      b.match(/view\/[^"]*?-(\d{8,})/)?.[1];
    const title = b.match(/base-search-card__title[^>]*>\s*([\s\S]*?)\s*<\//)?.[1];
    const company = b.match(/base-search-card__subtitle[^>]*>\s*<a[^>]*>\s*([\s\S]*?)\s*<\//)?.[1];
    const location = b.match(/job-search-card__location[^>]*>\s*([\s\S]*?)\s*<\//)?.[1];
    const posted = b.match(/listdate[^>]*datetime="([^"]+)"/)?.[1] ?? "";
    if (!id || !title) continue;
    cards.push({
      id,
      title: decode(title),
      company: decode(company ?? "?"),
      location: decode(location ?? "?"),
      posted,
      url: `https://www.linkedin.com/jobs/view/${id}`,
    });
  }
  return cards;
}

async function search(keywords: string, opts: { location?: string; remote?: boolean; days?: number; pages?: number }) {
  const all: JobCard[] = [];
  const pages = opts.pages ?? 1;
  for (let p = 0; p < pages; p++) {
    const q = new URLSearchParams({ keywords, start: String(p * 25) });
    if (opts.location) q.set("location", opts.location);
    if (opts.remote) q.set("f_WT", "2"); // 1=onsite 2=remote 3=hybrid
    if (opts.days) q.set("f_TPR", `r${opts.days * 86400}`); // 게시일 필터(초)
    const html = await fetchHtml(`${BASE}/seeMoreJobPostings/search?${q}`);
    const cards = parseCards(html);
    all.push(...cards);
    if (cards.length < 10) break; // 마지막 페이지
    await sleep(1500);
  }
  return all;
}

function htmlToMd(html: string): string {
  return decode(
    html
      // <strong> 안쪽의 br/공백을 밖으로 밀어내 볼드 깨짐 방지
      .replace(/<(strong|b)>([\s\S]*?)<\/\1>/gi, (_, __, inner) => {
        const trailingBreak = /<br\s*\/?>\s*$/i.test(inner) ? "\n\n" : "";
        const text = inner.replace(/<br\s*\/?>/gi, " ").replace(/<[^>]+>/g, "").trim();
        return text ? `**${text}**${trailingBreak}` : "";
      })
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<\/p>/gi, "\n\n")
      .replace(/<li[^>]*>/gi, "- ")
      .replace(/<\/li>/gi, "\n")
      .replace(/<[^>]+>/g, "")
      .replace(/[ \t]+\n/g, "\n")
      .replace(/\n{3,}/g, "\n\n"),
  );
}

async function jobDetail(idOrUrl: string) {
  const id = idOrUrl.match(/(\d{8,})/)?.[1];
  if (!id) throw new Error(`job id를 찾을 수 없음: ${idOrUrl}`);
  const html = await fetchHtml(`${BASE}/jobPosting/${id}`);
  const title = decode(html.match(/top-card-layout__title[^>]*>\s*([\s\S]*?)\s*<\//)?.[1] ?? "?");
  const company = decode(html.match(/topcard__org-name-link[^>]*>\s*([\s\S]*?)\s*<\//)?.[1] ?? "?");
  const location = decode(html.match(/topcard__flavor--bullet[^>]*>\s*([\s\S]*?)\s*<\//)?.[1] ?? "?");
  const posted = decode(html.match(/posted-time-ago__text[^>]*>\s*([\s\S]*?)\s*<\//)?.[1] ?? "?");
  const applicants = decode(html.match(/num-applicants__caption[^>]*>\s*([\s\S]*?)\s*<\//)?.[1] ?? "");
  const criteria = [...html.matchAll(/description-job-criteria-subheader[^>]*>\s*([\s\S]*?)\s*<\/h3>\s*<span[^>]*>\s*([\s\S]*?)\s*<\/span>/g)]
    .map((m) => `${decode(m[1])}: ${decode(m[2])}`);
  const descHtml = html.match(/show-more-less-html__markup[^>]*>([\s\S]*?)<\/div>/)?.[1] ?? "";
  return { id, title, company, location, posted, applicants, criteria, description: htmlToMd(descHtml), url: `https://www.linkedin.com/jobs/view/${id}` };
}

const slug = (s: string) =>
  s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60);

async function main() {
  const [cmd, ...rest] = Bun.argv.slice(2);
  const flag = (name: string) => {
    const i = rest.indexOf(`--${name}`);
    return i >= 0 ? rest[i + 1] : undefined;
  };

  if (cmd === "search") {
    const keywords = rest[0];
    if (!keywords || keywords.startsWith("--")) throw new Error("검색어 필요");
    const cards = await search(keywords, {
      location: flag("location"),
      remote: rest.includes("--remote"),
      days: flag("days") ? Number(flag("days")) : undefined,
      pages: flag("pages") ? Number(flag("pages")) : 1,
    });
    console.log(`| # | 포지션 | 회사 | 위치 | 게시일 | ID |`);
    console.log(`|---|--------|------|------|--------|-----|`);
    cards.forEach((c, i) =>
      console.log(`| ${i + 1} | [${c.title}](${c.url}) | ${c.company} | ${c.location} | ${c.posted} | ${c.id} |`),
    );
    console.error(`\n${cards.length}건. 상세: bun tools/linkedin-jobs.ts job <ID> · 인박스: … inbox <ID>`);
  } else if (cmd === "job" || cmd === "inbox") {
    const d = await jobDetail(rest[0]);
    const md = [
      `# ${d.company} — ${d.title}`,
      ``,
      `- 상태: inbox · 수집일: ${new Date().toISOString().slice(0, 10)} · 게시: ${d.posted}${d.applicants ? ` · ${d.applicants}` : ""}`,
      `- 소스: ${d.url}`,
      `- 위치: ${d.location}`,
      d.criteria.length ? `- 조건: ${d.criteria.join(" · ")}` : null,
      ``,
      `## 핏 점수: ? / 10`,
      `- (평가 전)`,
      ``,
      `## JD 전문`,
      ``,
      d.description,
      ``,
    ].filter((l) => l !== null).join("\n") + "\n";
    if (cmd === "inbox") {
      const path = `jobs/inbox/${slug(`${d.company}-${d.title}`)}.md`;
      await Bun.write(path, md);
      console.log(`저장됨: ${path}`);
    } else {
      console.log(md);
    }
  } else {
    console.error("사용법: search <keywords> [--location L] [--remote] [--days N] [--pages N] | job <id|url> | inbox <id|url>");
    process.exit(1);
  }
}

main();
