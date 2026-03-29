-- Sample SQL: accounts schema

CREATE TABLE dbo.Users (
    UserId      UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
    Username    NVARCHAR(128)    NOT NULL,
    Email       NVARCHAR(256)    NOT NULL,
    CreatedDate DATETIME         DEFAULT GETDATE(),
    IsEnabled   BIT              NOT NULL DEFAULT 1
);

CREATE TABLE dbo.Roles (
    RoleId   UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
    RoleName NVARCHAR(64)     NOT NULL
);

CREATE TABLE dbo.UserRoles (
    UserId UNIQUEIDENTIFIER NOT NULL,
    RoleId UNIQUEIDENTIFIER NOT NULL,
    CONSTRAINT FK_UserRoles_Users FOREIGN KEY (UserId) REFERENCES dbo.Users(UserId),
    CONSTRAINT FK_UserRoles_Roles FOREIGN KEY (RoleId) REFERENCES dbo.Roles(RoleId)
);

CREATE VIEW dbo.ActiveUsers AS
SELECT UserId, Username, Email
FROM dbo.Users
WHERE IsEnabled = 1;

CREATE PROCEDURE dbo.proc_GetUserById
    @UserId UNIQUEIDENTIFIER
AS
BEGIN
    SET NOCOUNT ON;
    SELECT UserId, Username, Email, CreatedDate
    FROM dbo.Users
    WHERE UserId = @UserId;
END;

CREATE OR ALTER PROCEDURE dbo.proc_UpdateUserEmail
    @UserId UNIQUEIDENTIFIER,
    @Email  NVARCHAR(256)
AS
BEGIN
    UPDATE dbo.Users SET Email = @Email WHERE UserId = @UserId;
END;
