() => {
    const rows = document.querySelectorAll('div[class*="flex"][class*="pt-3"][class*="pb-3"][class*="border-b"]');
    return Array.from(rows).map(row => {
        const link = row.querySelector('a[href*="/md5/"]');
        if (!link) return null;
        const href = link.getAttribute('href') || '';
        const md5m = href.match(/[a-f0-9]{32}/);
        if (!md5m) return null;

        const titleEl = row.querySelector('a[class*="line-clamp-[3]"]');
        const title = titleEl ? titleEl.innerText.trim() : '';

        const authorEls = row.querySelectorAll('a[class*="line-clamp-[2]"]');
        let author = '';
        for (const a of authorEls) {
            const t = a.innerText.trim();
            if (t.length > 0 && !t.startsWith('/') && !t.includes('\\')) {
                author = t;
                break;
            }
        }

        const metaDiv = row.querySelector('div[class*="text-gray-800"][class*="dark:text-slate-400"]');
        const metaText = metaDiv ? metaDiv.innerText.trim() : '';

        return {
            md5: md5m[0],
            title: title,
            author: author,
            metaText: metaText
        };
    }).filter(x => x !== null && x.md5);
}
