// rec director: just wait for the page to finish (window.__done), then stop.
await waitFor(() => window.__done === true, 600000, 'page done');
rec.log('page done; captions=' + (window.__captions || []).length);
