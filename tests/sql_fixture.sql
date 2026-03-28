-- Test fixture for SQL metadata extraction
-- Covers tables, views, stored procedures, functions, and references

CREATE TABLE dbo.Products (
    ProductId UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
    ProductName NVARCHAR(256) NOT NULL,
    Category NVARCHAR(512) NOT NULL,
    CreatedDate DATETIME DEFAULT GETDATE(),
    IsActive BIT NOT NULL DEFAULT 1
);

CREATE TABLE dbo.Orders (
    OrderId UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
    ProductId UNIQUEIDENTIFIER NOT NULL,
    OrderDate NVARCHAR(512) NOT NULL,
    Quantity INT,
    CONSTRAINT FK_Orders_Products FOREIGN KEY (ProductId) REFERENCES dbo.Products(ProductId)
);

CREATE VIEW dbo.ActiveProducts AS
SELECT ProductId, ProductName, Category
FROM dbo.Products
WHERE IsActive = 1;

CREATE PROCEDURE dbo.proc_GetProductById
    @ProductId UNIQUEIDENTIFIER
AS
BEGIN
    SET NOCOUNT ON;
    SELECT ProductId, ProductName, Category, CreatedDate
    FROM dbo.Products
    WHERE ProductId = @ProductId;
END;

CREATE PROCEDURE dbo.proc_InsertOrder
    @ProductId UNIQUEIDENTIFIER,
    @Quantity INT,
    @Note NVARCHAR(MAX)
AS
BEGIN
    INSERT INTO dbo.OrderLog (ProductId, Quantity, Note, OrderDate)
    VALUES (@ProductId, @Quantity, @Note, GETDATE());
END;

CREATE FUNCTION dbo.fn_GetProductCount()
RETURNS INT
AS
BEGIN
    RETURN (SELECT COUNT(*) FROM dbo.Products WHERE IsActive = 1);
END;

CREATE OR ALTER PROCEDURE dbo.proc_UpdateProduct
    @ProductId UNIQUEIDENTIFIER,
    @ProductName NVARCHAR(256)
AS
BEGIN
    UPDATE dbo.Products SET ProductName = @ProductName WHERE ProductId = @ProductId;
END;
