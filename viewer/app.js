// Data storage
const dataStore = {
    passed: null,
    nearmiss: null,
    all: null
};

let currentTab = 'passed';
let currentData = null;
let allCategories = new Set();

// Column name mapping (case-insensitive)
const columnMap = {
    title: ['title', 'name', 'item', 'product'],
    brand: ['brand'],
    category: ['category', 'categories'],
    buy_price: ['woot_price', 'buy', 'cost', 'price_buy', 'purchase_price'],
    sell_price: ['ebay_price', 'sold_price', 'sell', 'price_sell', 'avg_sold', 'expected_sale'],
    profit: ['net_profit', 'profit'],
    roi: ['roi', 'net_roi'],
    sold_comps: ['sold_comps', 'comps', 'sold_count'],
    url_source: ['woot_url', 'source_url', 'url'],
    url_ebay: ['ebay_url', 'comp_url', 'ebay_search_url']
};

// Normalize column names
function normalizeColumns(headers) {
    const normalized = {};
    const lowerHeaders = headers.map(h => h.toLowerCase().trim());
    
    Object.keys(columnMap).forEach(canonical => {
        const aliases = columnMap[canonical];
        for (let i = 0; i < headers.length; i++) {
            if (aliases.includes(lowerHeaders[i])) {
                normalized[canonical] = headers[i];
                break;
            }
        }
    });
    
    return normalized;
}

// Get field value from row
function getField(row, fieldName, columnMap) {
    const columnName = columnMap[fieldName];
    if (!columnName) return null;
    const value = row[columnName];
    return value === '' || value === null || value === undefined ? null : value;
}

// Format currency
function formatCurrency(value) {
    if (value === null || value === undefined || value === '') return '—';
    const num = parseFloat(value);
    if (isNaN(num)) return '—';
    return `$${num.toFixed(2)}`;
}

// Format ROI
function formatROI(value) {
    if (value === null || value === undefined || value === '') return '—';
    const num = parseFloat(value);
    if (isNaN(num)) return '—';
    
    // If ROI is between -1 and 1, treat as decimal (0.38 -> 38%)
    // Otherwise treat as percentage (38 -> 38%)
    if (num >= -1 && num <= 1) {
        return `${(num * 100).toFixed(1)}%`;
    }
    return `${num.toFixed(1)}%`;
}

// Format number
function formatNumber(value) {
    if (value === null || value === undefined || value === '') return '—';
    const num = parseFloat(value);
    if (isNaN(num)) return '—';
    return num.toString();
}

// Load CSV file
function loadCSV(file, tabName) {
    Papa.parse(file, {
        header: true,
        skipEmptyLines: true,
        complete: function(results) {
            if (results.errors.length > 0) {
                console.error('CSV parsing errors:', results.errors);
            }
            
            const rows = results.data;
            const columnMapping = normalizeColumns(results.meta.fields);
            
            // Process rows
            const processed = rows.map(row => {
                const item = {
                    title: getField(row, 'title', columnMapping) || '—',
                    brand: getField(row, 'brand', columnMapping),
                    category: getField(row, 'category', columnMapping),
                    buy_price: getField(row, 'buy_price', columnMapping),
                    sell_price: getField(row, 'sell_price', columnMapping),
                    profit: getField(row, 'profit', columnMapping),
                    roi: getField(row, 'roi', columnMapping),
                    sold_comps: getField(row, 'sold_comps', columnMapping),
                    url_source: getField(row, 'url_source', columnMapping),
                    url_ebay: getField(row, 'url_ebay', columnMapping),
                    _raw: row
                };
                
                return item;
            });
            
            dataStore[tabName] = {
                data: processed,
                filename: file.name
            };
            
            // Update category dropdown (collects from all tabs)
            updateCategoryDropdown();
            
            // If this is the current tab, display it
            if (currentTab === tabName) {
                currentData = processed;
                render();
            }
        },
        error: function(error) {
            alert('Error loading CSV: ' + error.message);
        }
    });
}

// Collect all categories from all loaded datasets
function collectAllCategories() {
    allCategories.clear();
    Object.values(dataStore).forEach(store => {
        if (store && store.data) {
            store.data.forEach(item => {
                if (item.category && item.category !== '—') {
                    allCategories.add(item.category);
                }
            });
        }
    });
}

// Update category dropdown
function updateCategoryDropdown() {
    collectAllCategories();
    const categorySelect = document.getElementById('category-select');
    const categoryGroup = document.getElementById('category-group');
    
    // Clear existing options (except "All Categories")
    categorySelect.innerHTML = '<option value="">All Categories</option>';
    
    if (allCategories.size > 0) {
        categoryGroup.style.display = 'block';
        const sortedCategories = Array.from(allCategories).sort();
        sortedCategories.forEach(cat => {
            const option = document.createElement('option');
            option.value = cat;
            option.textContent = cat;
            categorySelect.appendChild(option);
        });
    } else {
        categoryGroup.style.display = 'none';
    }
}

// Filter data
function filterData(data) {
    const searchTerm = document.getElementById('search-input').value.toLowerCase();
    const categoryFilter = document.getElementById('category-select').value;
    
    return data.filter(item => {
        // Search filter
        if (searchTerm) {
            const searchable = [
                item.title,
                item.brand,
                item.category
            ].filter(x => x && x !== '—').join(' ').toLowerCase();
            
            if (!searchable.includes(searchTerm)) {
                return false;
            }
        }
        
        // Category filter
        if (categoryFilter && item.category !== categoryFilter) {
            return false;
        }
        
        return true;
    });
}

// Sort data
function sortData(data) {
    const sortValue = document.getElementById('sort-select').value;
    const [field, direction] = sortValue.split('-');
    
    return [...data].sort((a, b) => {
        let aVal, bVal;
        
        switch (field) {
            case 'profit':
                aVal = a.profit ? parseFloat(a.profit) : -Infinity;
                bVal = b.profit ? parseFloat(b.profit) : -Infinity;
                break;
            case 'roi':
                aVal = a.roi ? parseFloat(a.roi) : -Infinity;
                bVal = b.roi ? parseFloat(b.roi) : -Infinity;
                break;
            case 'sold_comps':
                aVal = a.sold_comps ? parseFloat(a.sold_comps) : -Infinity;
                bVal = b.sold_comps ? parseFloat(b.sold_comps) : -Infinity;
                break;
            case 'price':
                aVal = a.buy_price ? parseFloat(a.buy_price) : -Infinity;
                bVal = b.buy_price ? parseFloat(b.buy_price) : -Infinity;
                break;
            default:
                return 0;
        }
        
        if (direction === 'desc') {
            return bVal - aVal;
        } else {
            return aVal - bVal;
        }
    });
}

// Render cards
function render() {
    if (!currentData) {
        document.getElementById('summary').textContent = 'No data loaded';
        document.getElementById('cards-grid').innerHTML = '<div class="empty-state">Load a CSV file to view reports</div>';
        return;
    }
    
    const filtered = filterData(currentData);
    const sorted = sortData(filtered);
    
    const store = dataStore[currentTab];
    const filename = store ? store.filename : 'No file';
    
    // Update summary
    document.getElementById('summary').innerHTML = 
        `Loaded: <strong>${filename}</strong> | Items: <strong>${currentData.length}</strong> | Showing: <strong>${sorted.length}</strong>`;
    
    // Render cards
    const grid = document.getElementById('cards-grid');
    
    if (sorted.length === 0) {
        grid.innerHTML = '<div class="empty-state">No items match your filters</div>';
        return;
    }
    
    grid.innerHTML = sorted.map(item => {
        const profit = parseFloat(item.profit);
        const profitClass = !isNaN(profit) ? (profit >= 0 ? 'profit-positive' : 'profit-negative') : '';
        
        return `
            <div class="card">
                <div class="card-title">${escapeHtml(item.title)}</div>
                <div class="badges">
                    ${item.brand && item.brand !== '—' ? `<span class="badge">${escapeHtml(item.brand)}</span>` : ''}
                    ${item.category && item.category !== '—' ? `<span class="badge">${escapeHtml(item.category)}</span>` : ''}
                </div>
                <div class="metrics">
                    <div class="metric">
                        <div class="metric-label">Profit</div>
                        <div class="metric-value ${profitClass}">${formatCurrency(item.profit)}</div>
                    </div>
                    <div class="metric">
                        <div class="metric-label">ROI</div>
                        <div class="metric-value">${formatROI(item.roi)}</div>
                    </div>
                    <div class="metric">
                        <div class="metric-label">Sold Comps</div>
                        <div class="metric-value">${formatNumber(item.sold_comps)}</div>
                    </div>
                    <div class="metric">
                        <div class="metric-label">Buy</div>
                        <div class="metric-value">${formatCurrency(item.buy_price)}</div>
                    </div>
                    <div class="metric">
                        <div class="metric-label">Sell</div>
                        <div class="metric-value">${formatCurrency(item.sell_price)}</div>
                    </div>
                </div>
                <div class="links">
                    ${item.url_source && item.url_source !== '—' ? `<a href="${escapeHtml(item.url_source)}" target="_blank" rel="noopener noreferrer" class="link">Source</a>` : ''}
                    ${item.url_ebay && item.url_ebay !== '—' ? `<a href="${escapeHtml(item.url_ebay)}" target="_blank" rel="noopener noreferrer" class="link">eBay comps</a>` : ''}
                </div>
            </div>
        `;
    }).join('');
}

// Escape HTML
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Event listeners
document.getElementById('file-input').addEventListener('change', function(e) {
    const file = e.target.files[0];
    if (file) {
        loadCSV(file, currentTab);
    }
});

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', function() {
        // Update active tab
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        this.classList.add('active');
        currentTab = this.dataset.tab;
        
        // Load data for this tab if available
        if (dataStore[currentTab]) {
            currentData = dataStore[currentTab].data;
            updateCategoryDropdown(); // Update categories when switching tabs
            render();
        } else {
            // Prompt to load file
            const fileInput = document.getElementById('file-input');
            fileInput.click();
        }
    });
});

document.getElementById('search-input').addEventListener('input', render);
document.getElementById('sort-select').addEventListener('change', render);
document.getElementById('category-select').addEventListener('change', render);

// Initialize
render();

